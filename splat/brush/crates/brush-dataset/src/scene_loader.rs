use std::sync::Arc;

use brush_async::Actor;
use burn::tensor::{Device, Tensor, TensorData};
use rand::{SeedableRng, seq::SliceRandom};
use tokio::sync::{Mutex, mpsc};

use crate::{
    config::LoadDatasetConfig,
    scene::{Scene, SceneBatch, view_to_packed_data},
};

/// Shared cache of GPU-ready scene batches. Each slot holds at most one
/// batch; once the running total passes `budget_bytes`, new batches bypass
/// the cache and just get re-decoded + re-packed on every visit.
///
/// Caching the packed batch (instead of the decoded `DynamicImage`) skips
/// the per-hit decode → premultiply → repack work. Cached buffers are put
/// behind a refcount first (see `share_packed`), so a hit doesn't copy the
/// pixels either: it hands out a view of the same allocation.
struct BatchCache {
    slots: Vec<Option<Arc<SceneBatch>>>,
    used_bytes: u64,
    budget_bytes: u64,
}

impl BatchCache {
    fn new(n_views: usize, budget_bytes: u64) -> Self {
        Self {
            slots: vec![None; n_views],
            used_bytes: 0,
            budget_bytes,
        }
    }

    fn get(&self, index: usize) -> Option<Arc<SceneBatch>> {
        self.slots[index].clone()
    }

    /// Whether `insert` would take this batch: nothing cached for the view
    /// yet, and it still fits the budget. Checked before caching so the
    /// packed bytes only get shared when they're actually going to be kept.
    ///
    /// Tracks exact bytes: rounding to whole MB let sub-MB images slip in
    /// for free and bypass the budget entirely.
    fn admits(&self, index: usize, batch: &SceneBatch) -> bool {
        self.slots[index].is_none() && self.used_bytes + batch.packed_bytes() < self.budget_bytes
    }

    fn insert(&mut self, index: usize, batch: Arc<SceneBatch>) {
        if !self.admits(index, &batch) {
            return;
        }
        self.used_bytes += batch.packed_bytes();
        self.slots[index] = Some(batch);
    }
}

pub struct SceneLoader {
    rx: mpsc::Receiver<SceneBatch>,
    // Owns the loader actor threads. Dropping cancels them; their
    // senders then drop, the channel closes, and `next_batch` returns.
    _actors: Vec<Actor>,
}

impl SceneLoader {
    pub fn new(scene: &Scene, seed: u64, config: &LoadDatasetConfig) -> Self {
        Self::with_device(scene, seed, config, None)
    }

    /// `device` enables GPU-resident images: each view is uploaded once at
    /// decode time and the cached batch hands the trainer a ready tensor,
    /// so a step costs zero decode and zero copy after the first epoch.
    pub fn with_device(
        scene: &Scene,
        seed: u64,
        config: &LoadDatasetConfig,
        device: Option<Device>,
    ) -> Self {
        // Prefetch buffer: at most 4 batches ahead of the trainer.
        // Two tasks per actor share this buffer so one task's I/O can
        // overlap with the other's decode + GPU upload.
        let (tx, rx) = mpsc::channel(4);

        // Fan out only as many loaders as we have real parallelism.
        // Wasm shares one JS event loop, so extra actors just add
        // contention without overlapping I/O.
        let n_actors = if cfg!(target_family = "wasm") {
            1
        } else {
            std::thread::available_parallelism().map_or(8, |p| p.get())
        };
        const TASKS_PER_ACTOR: usize = 2;

        let views = scene.views.clone();
        let cache = Arc::new(Mutex::new(BatchCache::new(
            views.len(),
            config.max_scene_batch_cache_size,
        )));

        // Cold-start prefetch: partition view indices round-robin across all
        // tasks so every view is decoded + packed exactly once, as early as
        // the worker pool can get to it. Without this, each task shuffles
        // independently and the same view is decoded several times during
        // the first epochs — pure waste on phone-size captures where the
        // whole dataset fits the cache. Once a task's stripe is drained it
        // falls back to the infinite shuffled stream.
        let n_tasks = (n_actors * TASKS_PER_ACTOR) as u64;
        let inner_device = device.map(|d| d.inner());
        let device = inner_device.filter(|_| {
            // GPU residency is a native-only win; wasm keeps host batches.
            !cfg!(target_family = "wasm")
        });

        let mut task_idx: u64 = 0;
        let actors: Vec<Actor> = (0..n_actors)
            .map(|i| {
                let actor = Actor::new(&format!("dataloader-{i}"));
                for _ in 0..TASKS_PER_ACTOR {
                    let views = views.clone();
                    let cache = cache.clone();
                    let tx = tx.clone();
                    let task_seed = seed.wrapping_add(task_idx);
                    let init: Vec<usize> = (0..views.len())
                        .filter(|i| *i as u64 % n_tasks == task_idx)
                        .collect();
                    let device = device.clone();
                    task_idx += 1;
                    actor
                        .run(move || run_loader(views, cache, tx, task_seed, init, device))
                        .detach();
                }
                actor
            })
            .collect();

        Self {
            rx,
            _actors: actors,
        }
    }

    pub async fn next_batch(&mut self) -> SceneBatch {
        self.rx
            .recv()
            .await
            .expect("Scene loader channel closed unexpectedly")
    }
}

async fn run_loader(
    views: Arc<Vec<crate::scene::SceneView>>,
    cache: Arc<Mutex<BatchCache>>,
    tx: mpsc::Sender<SceneBatch>,
    seed: u64,
    init: Vec<usize>,
    device: Option<Device>,
) {
    let mut rng = rand::rngs::StdRng::seed_from_u64(seed);
    let mut shuffled: Vec<usize> = Vec::new();
    let mut init = init.into_iter();

    loop {
        let index = if let Some(i) = init.next() {
            i
        } else {
            if shuffled.is_empty() {
                shuffled = (0..views.len()).collect();
                shuffled.shuffle(&mut rng);
            }
            shuffled.pop().expect("Need at least one view in dataset")
        };
        let view = &views[index];

        let cached = cache.lock().await.get(index);

        let batch = if let Some(batch) = cached {
            // The cached buffer is refcounted, so this is a pointer bump
            // rather than a copy of the whole image.
            batch.as_ref().clone()
        } else {
            let raw = view
                .image
                .load()
                .await
                .expect("Scene loader failed to load an image");
            let (img_packed, has_alpha) = view_to_packed_data(raw, view.image.alpha_mode());
            let mut batch = SceneBatch {
                // Share the pixels up front: the cache copy, the GPU upload
                // below and every later hit then cost a refcount instead of
                // a copy of the whole image.
                img_packed: share_packed(img_packed),
                img_gpu: None,
                has_alpha,
                alpha_mode: view.image.alpha_mode(),
                camera: view.camera,
                view_index: index as u32,
            };

            let mut cache = cache.lock().await;
            if cache.admits(index, &batch) {
                if let Some(d) = device.as_ref() {
                    // Upload once, at decode time: later epochs reuse the
                    // resident tensor and the trainer never copies again.
                    // The TensorData clone is a refcount bump on the shared
                    // bytes, not a pixel copy.
                    batch.img_gpu = Some(Tensor::from_data(batch.img_packed.clone(), d));
                }
                cache.insert(index, Arc::new(batch.clone()));
            }
            batch
        };

        if tx.send(batch).await.is_err() {
            break;
        }
        brush_async::yield_now().await;
    }
}

/// Move the packed pixels behind a refcount, so cloning the batch out of the
/// cache doesn't copy them. Uploading to the GPU is unaffected: that copies
/// into a staging buffer either way.
fn share_packed(data: TensorData) -> TensorData {
    TensorData::from_bytes(data.bytes.shared(), data.shape, data.dtype)
}
