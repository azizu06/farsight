/** Illustrative desk eyes, never a replacement for tracked headset provenance. */
export class DesktopPreviewCamera {
  constructor({ THREE, camera, width, height, session = `desktop-preview-${crypto.randomUUID()}` }) {
    this.THREE = THREE; this.camera = camera; this.session = session; this.frame = 0;
    this.aspect = width / 2 / height;
    this.eyeCamera = new THREE.PerspectiveCamera(camera.fov, this.aspect, camera.near, camera.far);
  }
  next() {
    const { THREE, camera, eyeCamera } = this;
    eyeCamera.fov = camera.fov; eyeCamera.near = camera.near; eyeCamera.far = camera.far;
    eyeCamera.updateProjectionMatrix();
    const eyes = ["left", "right"].map((eye, i) => {
      const offset = new THREE.Vector3(i ? .032 : -.032, 0, 0).applyQuaternion(camera.quaternion);
      return { eye, position: camera.position.clone().add(offset).toArray(),
        quaternion: camera.quaternion.toArray(), projection: eyeCamera.projectionMatrix.toArray() };
    });
    return { session_id: this.session, frame_id: ++this.frame, desktop_preview: true,
      space: "ar_map", fov_y_deg: camera.fov, aspect: this.aspect, eyes };
  }
}
