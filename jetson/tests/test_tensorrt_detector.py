"""CPU policy/resource tests. These mocks do not verify CUDA or GPU performance."""

import ctypes
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

from jetson.tensorrt_detector import (CudaRuntime, TensorRTDetector, TensorRTError,
                                      INPUT_SHAPE, OUTPUT_SHAPE, POSE_OUTPUT_SHAPE, MAX_BUFFER_BYTES, allocation_size,
                                      DEPTH_INPUT_SHAPE, DEPTH_OUTPUT_SHAPE)


class FakeCuda:
    def __init__(self):
        self.buffers, self.host, self.events = {}, {}, []
        self.next_pointer = 100
        self.fail_copy = False

    def set_device(self, device):
        self.events.append(("device", device))

    def malloc(self, size):
        self.next_pointer += 1
        self.buffers[self.next_pointer] = bytearray(size)
        return self.next_pointer

    def host_array(self, shape, dtype):
        array = np.zeros(shape, dtype=dtype)
        self.host[array.ctypes.data] = array
        return array, array.ctypes.data

    def stream(self):
        self.events.append(("stream", 7))
        return 7

    def copy(self, destination, source, size, kind, stream):
        self.events.append(("copy", kind, stream))
        if self.fail_copy:
            raise TensorRTError("copy failed")
        if kind == 1:
            self.buffers[destination][:] = ctypes.string_at(source, size)
        else:
            source_bytes = bytes(self.buffers[source])
            ctypes.memmove(destination, source_bytes, size)

    def synchronize(self, stream):
        self.events.append(("sync", stream))

    def free(self, pointer):
        self.events.append(("free", pointer))
        del self.buffers[pointer]

    def free_host(self, pointer):
        self.events.append(("free_host", pointer))
        del self.host[pointer]

    def destroy_stream(self, stream):
        self.events.append(("destroy", stream))


class FakeContext:
    def __init__(self, engine):
        self.engine = engine
        self.input_shape = engine.input_shape
        self.addresses = {}
        self.execution_ok = True
        self.shape_ok = True
        self.missing = []

    def set_input_shape(self, name, shape):
        self.input_shape = tuple(shape)
        return self.shape_ok

    def infer_shapes(self):
        return self.missing

    def get_tensor_shape(self, name):
        return self.input_shape if name == "images" else self.engine.output_shape

    def set_tensor_address(self, name, pointer):
        self.addresses[name] = pointer
        return self.engine.address_ok

    def execute_async_v3(self, stream_handle):
        self.engine.cuda.events.append(("execute", stream_handle))
        if self.execution_ok:
            prediction = np.zeros(self.engine.output_shape, dtype=self.engine.dtype)
            prediction[:, :4, :] = 100
            prediction[:, 4, :] = self.engine.score
            if self.engine.output_shape == POSE_OUTPUT_SHAPE:
                prediction[:, 5::3, :] = 250  # keypoint x/y coordinates, not class scores
                prediction[:, 6::3, :] = 300
                prediction[:, 7::3, :] = .8
            if self.engine.nonfinite:
                prediction[0, 0, 0] = np.nan
            self.engine.cuda.buffers[self.addresses["output0"]][:] = prediction.tobytes()
        return self.execution_ok


class FakeEngine:
    num_io_tensors = 2

    def __init__(self, cuda):
        self.cuda = cuda
        self.input_shape = INPUT_SHAPE
        self.output_shape = OUTPUT_SHAPE
        self.dtype = np.float32
        self.location = "device"
        self.format = "linear"
        self.score = .7
        self.nonfinite = False
        self.address_ok = True
        self.context = FakeContext(self)

    def get_tensor_name(self, index):
        return ("images", "output0")[index]

    def get_tensor_mode(self, name):
        return "input" if name == "images" else "output"

    def get_tensor_shape(self, name):
        return self.input_shape if name == "images" else self.output_shape

    def get_tensor_dtype(self, name):
        return self.dtype

    def get_tensor_format(self, name):
        return self.format

    def get_tensor_location(self, name):
        return self.location

    def is_shape_inference_io(self, name):
        return False

    def create_execution_context(self):
        return self.context


def fake_trt(engine):
    class Logger:
        WARNING = 2
        def __init__(self, severity):
            pass
    return types.SimpleNamespace(__version__="10.13.2", Logger=Logger,
        Runtime=lambda logger: types.SimpleNamespace(deserialize_cuda_engine=lambda data: engine),
        init_libnvinfer_plugins=lambda logger, namespace: True,
        TensorIOMode=types.SimpleNamespace(INPUT="input", OUTPUT="output"),
        TensorLocation=types.SimpleNamespace(DEVICE="device"),
        TensorFormat=types.SimpleNamespace(LINEAR="linear"), nptype=lambda dtype: dtype)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.engine"
        self.path.write_bytes(b"MOCK: no GPU verification")
        self.cuda = FakeCuda()
        self.engine = FakeEngine(self.cuda)
        self.trt = fake_trt(self.engine)

    def tearDown(self):
        self.temp.cleanup()

    def create(self):
        return TensorRTDetector(self.path, cuda=self.cuda, trt_module=self.trt, warmup=1)

    def test_metric_depth_contract_accepts_imagenet_input_and_metre_output(self):
        self.engine.input_shape = DEPTH_INPUT_SHAPE
        self.engine.output_shape = DEPTH_OUTPUT_SHAPE
        self.engine.context.input_shape = DEPTH_INPUT_SHAPE
        with TensorRTDetector(self.path, cuda=self.cuda, trt_module=self.trt, warmup=1,
                input_shape=DEPTH_INPUT_SHAPE, output_shape=DEPTH_OUTPUT_SHAPE, score_channels=0) as runner:
            output = runner.predict(np.full(DEPTH_INPUT_SHAPE, -2, np.float32))
            self.assertEqual(output.shape, DEPTH_OUTPUT_SHAPE)
            self.assertEqual(output[0, 0, 0], 100)  # metres aren't class probabilities
        self.assertEqual(self.cuda.buffers, {})
        self.assertEqual(self.cuda.host, {})

    def test_execution_order_warmup_and_detached_output(self):
        detector = self.create()
        relevant = [event[0:2] for event in self.cuda.events if event[0] in ("copy", "execute", "sync")]
        self.assertEqual(relevant, [("copy", 1), ("execute", 7), ("copy", 2), ("sync", 7)])
        result = detector.predict(np.full(INPUT_SHAPE, .5, dtype=np.float32))
        self.assertEqual(result.shape, OUTPUT_SHAPE)
        self.assertEqual(result.dtype, np.float32)
        self.assertAlmostEqual(result[0, 4, 0], .7, places=6)
        result[0, 4, 0] = 0
        self.assertAlmostEqual(detector.host_output[0, 4, 0], .7, places=6)
        input_values = np.frombuffer(self.cuda.buffers[detector.device_buffers[0]], dtype=np.float32)
        self.assertTrue(np.all(input_values == .5))
        detector.close()
        detector.close()
        self.assertEqual(self.cuda.buffers, {})
        self.assertEqual(self.cuda.host, {})
        with self.assertRaises(TensorRTError):
            detector.predict(np.zeros(INPUT_SHAPE, dtype=np.float32))

    def test_dynamic_input_resolves_640_and_fp16_buffers(self):
        self.engine.input_shape = (-1, 3, -1, -1)
        self.engine.context.input_shape = self.engine.input_shape
        self.engine.dtype = np.float16
        with self.create() as detector:
            self.assertEqual(self.engine.context.input_shape, INPUT_SHAPE)
            self.assertEqual(detector.host_input.dtype, np.float16)
            self.assertEqual(detector.predict(np.ones(INPUT_SHAPE, dtype=np.float32)).shape, OUTPUT_SHAPE)

    def test_wrong_output_and_layout_rejected_before_allocation(self):
        for attribute, value in (("output_shape", (1, 85, 8400)), ("output_shape", (1, 8400, 84)),
                                 ("output_shape", (1, 84, -1)), ("format", "chw16"),
                                 ("location", "host"), ("dtype", np.int8)):
            with self.subTest(attribute=attribute, value=value):
                original = getattr(self.engine, attribute)
                setattr(self.engine, attribute, value)
                with self.assertRaises(TensorRTError):
                    self.create()
                self.assertEqual(self.cuda.buffers, {})
                setattr(self.engine, attribute, original)

    def test_invalid_engine_version_and_deserialization(self):
        self.trt.__version__ = "8.6.2"
        with self.assertRaisesRegex(TensorRTError, "10"):
            self.create()
        with self.assertRaisesRegex(TensorRTError, "deserialization"):
            TensorRTDetector(self.path, cuda=self.cuda, trt_module=fake_trt(None))
        self.path.write_bytes(b"")
        with self.assertRaisesRegex(TensorRTError, "empty"):
            self.create()

    def test_missing_tensorrt_is_explicit_failure(self):
        with mock.patch("jetson.tensorrt_detector.importlib.import_module", side_effect=ImportError("missing")):
            with self.assertRaisesRegex(TensorRTError, "bindings unavailable"):
                TensorRTDetector(self.path)

    def test_unresolved_shape_and_rejected_address_release_resources(self):
        self.engine.context.missing = ["images"]
        with self.assertRaisesRegex(TensorRTError, "resolved"):
            self.create()
        self.engine.context.missing = []
        self.engine.address_ok = False
        with self.assertRaisesRegex(TensorRTError, "address"):
            self.create()
        self.assertEqual(self.cuda.buffers, {})

    def test_partial_pinned_allocation_failure_releases_previous_allocations(self):
        allocate = self.cuda.host_array
        calls = 0
        def limited(shape, dtype):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TensorRTError("cudaMallocHost out of memory")
            return allocate(shape, dtype)
        self.cuda.host_array = limited
        with self.assertRaisesRegex(TensorRTError, "out of memory"):
            self.create()
        self.assertEqual(self.cuda.buffers, {})
        self.assertEqual(self.cuda.host, {})

    def test_invalid_scores_and_nonfinite_warmup_release_resources(self):
        for score, nonfinite in ((2., False), (.7, True)):
            with self.subTest(score=score, nonfinite=nonfinite):
                self.engine.score, self.engine.nonfinite = score, nonfinite
                with self.assertRaises(TensorRTError):
                    self.create()
                self.assertEqual(self.cuda.buffers, {})
                self.assertEqual(self.cuda.host, {})

    def test_invalid_input_does_not_enqueue_or_destroy_valid_detector(self):
        with self.create() as detector:
            baseline = len(self.cuda.events)
            for array in (np.zeros((1, 3, 320, 320), dtype=np.float32),
                          np.zeros(INPUT_SHAPE, dtype=np.uint8), np.full(INPUT_SHAPE, 255, dtype=np.float32),
                          np.full(INPUT_SHAPE, np.nan, dtype=np.float32)):
                with self.assertRaises(ValueError):
                    detector.predict(array)
            self.assertEqual(len(self.cuda.events), baseline)
            self.assertFalse(detector.closed)

    def test_pose_is_explicit_and_keypoint_coordinates_are_not_class_probabilities(self):
        self.engine.output_shape = POSE_OUTPUT_SHAPE
        with self.assertRaisesRegex(TensorRTError, "84"):
            self.create()  # default contract remains strict COCO detection
        for dtype in (np.float32, np.float16):
            with self.subTest(dtype=dtype):
                self.engine.dtype = dtype
                with TensorRTDetector(self.path, cuda=self.cuda, trt_module=self.trt, warmup=1,
                                      output_shape=POSE_OUTPUT_SHAPE, score_channels=1) as detector:
                    result = detector.predict(np.zeros(INPUT_SHAPE, dtype=np.float32))
                    self.assertEqual(result.shape, POSE_OUTPUT_SHAPE)
                    self.assertEqual(detector.host_output.dtype, dtype)
                    self.assertEqual(result[0, 5, 0], 250)
        self.assertEqual(self.cuda.buffers, {})
        self.assertEqual(self.cuda.host, {})

    def test_pose_person_score_is_still_checked_and_contract_mismatch_rejected(self):
        self.engine.output_shape = POSE_OUTPUT_SHAPE
        self.engine.score = 2
        with self.assertRaisesRegex(TensorRTError, "probabilities"):
            TensorRTDetector(self.path, cuda=self.cuda, trt_module=self.trt, warmup=1,
                             output_shape=POSE_OUTPUT_SHAPE, score_channels=1)
        self.assertEqual(self.cuda.buffers, {})
        for shape, channels in ((POSE_OUTPUT_SHAPE, 80), (OUTPUT_SHAPE, 1),
                                ((1, 57, 8400), 1), (POSE_OUTPUT_SHAPE, True)):
            with self.subTest(shape=shape, channels=channels), self.assertRaises(ValueError):
                TensorRTDetector(self.path, output_shape=shape, score_channels=channels)

    def test_enqueue_and_copy_failure_close_device_resources(self):
        for failure in ("execute", "copy"):
            self.engine.context.execution_ok = True
            self.cuda.fail_copy = False
            detector = self.create()
            if failure == "execute":
                self.engine.context.execution_ok = False
            else:
                self.cuda.fail_copy = True
            with self.assertRaises(TensorRTError):
                detector.predict(np.zeros(INPUT_SHAPE, dtype=np.float32))
            self.assertTrue(detector.closed)
            self.assertEqual(self.cuda.buffers, {})
            self.assertEqual(self.cuda.host, {})

    def test_memory_bounds_reject_dynamic_and_oversized_shapes(self):
        self.assertEqual(allocation_size((1, 3, 640, 640), np.float32), 4915200)
        for shape in ((), (1, -1), (0, 10), (MAX_BUFFER_BYTES, 2)):
            with self.assertRaises(TensorRTError):
                allocation_size(shape, np.float32)


class Function:
    def __init__(self, action):
        self.action = action
    def __call__(self, *args):
        return self.action(*args)


class CudaBindingTests(unittest.TestCase):
    def test_missing_runtime_and_symbols_fail_explicitly(self):
        with mock.patch("jetson.tensorrt_detector.ctypes.CDLL", side_effect=OSError("not installed")):
            with self.assertRaisesRegex(TensorRTError, "libcudart unavailable"):
                CudaRuntime()
        with self.assertRaisesRegex(TensorRTError, "symbol"):
            CudaRuntime(library=types.SimpleNamespace())

    def test_checked_ctypes_calls_and_pinned_memory(self):
        retained = {}
        def allocate(pointer, size):
            buffer = ctypes.create_string_buffer(size)
            address = ctypes.addressof(buffer)
            retained[address] = buffer
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = address
            return 0
        def free(pointer):
            del retained[pointer]
            return 0
        def count(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int))[0] = 1
            return 0
        def stream(pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 7
            return 0
        lib = types.SimpleNamespace(**{name: Function(action) for name, action in {
            "cudaGetDeviceCount": count, "cudaSetDevice": lambda device: 0,
            "cudaMalloc": allocate, "cudaMallocHost": allocate, "cudaFree": free, "cudaFreeHost": free,
            "cudaMemcpyAsync": lambda dst, src, size, kind, stream: ctypes.memmove(dst, src, size) and 0,
            "cudaStreamCreate": stream, "cudaStreamSynchronize": lambda stream: 0,
            "cudaStreamDestroy": lambda stream: 0, "cudaGetErrorString": lambda code: b"out of memory",
        }.items()})
        cuda = CudaRuntime(library=lib)
        self.assertEqual(lib.cudaMemcpyAsync.argtypes[2], ctypes.c_size_t)
        self.assertEqual(lib.cudaMalloc.argtypes[0], ctypes.POINTER(ctypes.c_void_p))
        cuda.set_device(0)
        with self.assertRaisesRegex(TensorRTError, "unavailable"):
            cuda.set_device(1)
        array, pointer = cuda.host_array((1, 4), np.float32)
        array[:] = 3
        device = cuda.malloc(array.nbytes)
        cuda.copy(device, pointer, array.nbytes, 1, cuda.stream())
        self.assertEqual(ctypes.string_at(device, array.nbytes), array.tobytes())
        cuda.free(device)
        cuda.free_host(pointer)
        self.assertEqual(retained, {})
        lib.cudaMalloc.action = lambda pointer, size: 2
        with self.assertRaisesRegex(TensorRTError, "out of memory"):
            cuda.malloc(16)


if __name__ == "__main__":
    unittest.main()
