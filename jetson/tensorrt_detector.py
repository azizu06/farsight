"""TensorRT 10+ YOLOv8/11 COCO engine inference without PyTorch or PyCUDA.

Input is a pre-letterboxed RGB NCHW float array [1,3,640,640] in [0,1].
Default output is raw YOLO detection data [1,84,8400], before NMS. An explicit
pose contract supports [1,56,8400] with one person score and 17 keypoints. Engine execution
requires a real CUDA device; import alone works on development machines.
"""

import argparse
from contextlib import suppress
import ctypes
import ctypes.util
import importlib
import math
from pathlib import Path
import threading
import time

import numpy as np

INPUT_SHAPE = (1, 3, 640, 640)
OUTPUT_SHAPE = (1, 84, 8400)
POSE_OUTPUT_SHAPE = (1, 56, 8400)
DEPTH_INPUT_SHAPE = (1, 3, 308, 406)
DEPTH_OUTPUT_SHAPE = (1, 308, 406)
MAX_BUFFER_BYTES = 64 * 1024 * 1024
MAX_ENGINE_BYTES = 512 * 1024 * 1024


class TensorRTError(RuntimeError):
    """Unavailable or invalid GPU inference; callers must report this explicitly."""


def allocation_size(shape, dtype):
    if not shape or any(type(value) is not int or value <= 0 for value in shape):
        raise TensorRTError(f"Unresolved or invalid tensor dimensions: {shape}")
    size = math.prod(shape) * np.dtype(dtype).itemsize
    if not 0 < size <= MAX_BUFFER_BYTES:
        raise TensorRTError(f"Tensor allocation exceeds {MAX_BUFFER_BYTES} byte limit")
    return size


class CudaRuntime:
    """Minimal checked CUDA runtime bindings; uses the device primary context."""

    def __init__(self, library=None):
        if library is not None:
            self.lib = library
        else:
            candidates = [ctypes.util.find_library("cudart"), "libcudart.so.13", "libcudart.so.12",
                          "libcudart.so", "/usr/local/cuda/lib64/libcudart.so",
                          "/usr/local/cuda/targets/aarch64-linux/lib/libcudart.so"]
            failures = []
            for candidate in dict.fromkeys(value for value in candidates if value):
                try:
                    self.lib = ctypes.CDLL(candidate)
                    break
                except OSError as exc:
                    failures.append(str(exc))
            else:
                raise TensorRTError("CUDA runtime libcudart unavailable; use the JetPack CUDA environment. "
                                    + "; ".join(failures[-2:]))
        signatures = {
            "cudaGetDeviceCount": ([ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            "cudaSetDevice": ([ctypes.c_int], ctypes.c_int),
            "cudaMalloc": ([ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t], ctypes.c_int),
            "cudaFree": ([ctypes.c_void_p], ctypes.c_int),
            "cudaMallocHost": ([ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t], ctypes.c_int),
            "cudaFreeHost": ([ctypes.c_void_p], ctypes.c_int),
            "cudaMemcpyAsync": ([ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                 ctypes.c_int, ctypes.c_void_p], ctypes.c_int),
            "cudaStreamCreate": ([ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
            "cudaStreamSynchronize": ([ctypes.c_void_p], ctypes.c_int),
            "cudaStreamDestroy": ([ctypes.c_void_p], ctypes.c_int),
            "cudaGetErrorString": ([ctypes.c_int], ctypes.c_char_p),
        }
        try:
            for name, (arguments, result) in signatures.items():
                function = getattr(self.lib, name)
                function.argtypes, function.restype = arguments, result
        except AttributeError as exc:
            raise TensorRTError(f"CUDA runtime is missing a required symbol: {exc}") from exc

    def check(self, result, operation):
        if result:
            error = self.lib.cudaGetErrorString(result)
            description = error.decode(errors="replace") if error else "unknown error"
            raise TensorRTError(f"{operation}: CUDA error {result} ({description})")

    def set_device(self, device):
        count = ctypes.c_int()
        self.check(self.lib.cudaGetDeviceCount(ctypes.byref(count)), "cudaGetDeviceCount")
        if not 0 <= device < count.value:
            raise TensorRTError(f"CUDA device {device} unavailable; detected {count.value} device(s)")
        self.check(self.lib.cudaSetDevice(device), "cudaSetDevice")

    def malloc(self, size):
        if not 0 < size <= MAX_BUFFER_BYTES:
            raise TensorRTError("Invalid CUDA allocation size")
        pointer = ctypes.c_void_p()
        self.check(self.lib.cudaMalloc(ctypes.byref(pointer), size), "cudaMalloc")
        if not pointer.value:
            raise TensorRTError("cudaMalloc returned a null pointer")
        return pointer.value

    def host_array(self, shape, dtype):
        size = allocation_size(shape, dtype)
        pointer = ctypes.c_void_p()
        self.check(self.lib.cudaMallocHost(ctypes.byref(pointer), size), "cudaMallocHost")
        if not pointer.value:
            raise TensorRTError("cudaMallocHost returned a null pointer")
        try:
            backing = (ctypes.c_byte * size).from_address(pointer.value)
            return np.frombuffer(backing, dtype=dtype).reshape(shape), pointer.value
        except Exception:
            self.free_host(pointer.value)
            raise

    def stream(self):
        pointer = ctypes.c_void_p()
        self.check(self.lib.cudaStreamCreate(ctypes.byref(pointer)), "cudaStreamCreate")
        if not pointer.value:
            raise TensorRTError("cudaStreamCreate returned a null handle")
        return pointer.value

    def copy(self, destination, source, size, kind, stream):
        self.check(self.lib.cudaMemcpyAsync(destination, source, size, kind, stream), "cudaMemcpyAsync")

    def synchronize(self, stream):
        self.check(self.lib.cudaStreamSynchronize(stream), "cudaStreamSynchronize")

    def free(self, pointer):
        self.check(self.lib.cudaFree(pointer), "cudaFree")

    def free_host(self, pointer):
        self.check(self.lib.cudaFreeHost(pointer), "cudaFreeHost")

    def destroy_stream(self, stream):
        self.check(self.lib.cudaStreamDestroy(stream), "cudaStreamDestroy")


class TensorRTDetector:
    def __init__(self, engine_path, *, device=0, warmup=2, cuda=None, trt_module=None,
                 output_shape=OUTPUT_SHAPE, score_channels=80, input_shape=INPUT_SHAPE):
        self.lock = threading.RLock()
        self.device, self.cuda, self.stream = device, cuda, None
        self.device_buffers, self.host_buffers = [], []
        self.context = self.engine = self.runtime = self.logger = None
        self.closed = False
        if type(device) is not int or device < 0 or type(warmup) is not int or not 1 <= warmup <= 20:
            raise ValueError("Device must be nonnegative; warmup must be 1..20")
        if (not isinstance(output_shape, tuple) or any(type(value) is not int for value in output_shape)
                or type(score_channels) is not int
                or (input_shape, output_shape, score_channels) not in
                ((INPUT_SHAPE, OUTPUT_SHAPE, 80), (INPUT_SHAPE, POSE_OUTPUT_SHAPE, 1),
                 (DEPTH_INPUT_SHAPE, DEPTH_OUTPUT_SHAPE, 0))):
            raise ValueError("Output contract must be COCO detection (1,84,8400)/80 scores or pose (1,56,8400)/1 score")
        self.output_shape, self.score_channels = output_shape, score_channels
        self.input_shape = input_shape
        path = Path(engine_path)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_ENGINE_BYTES:
            raise TensorRTError("Engine missing, empty, or exceeds the 512 MiB size limit")
        try:
            trt = trt_module or importlib.import_module("tensorrt")
            if int(trt.__version__.split(".")[0]) < 10:
                raise TensorRTError("TensorRT 10+ is required for name-based engine execution")
            self.cuda = cuda or CudaRuntime()
            self.cuda.set_device(device)
            self.logger = trt.Logger(trt.Logger.WARNING)
            if hasattr(trt, "init_libnvinfer_plugins") and not trt.init_libnvinfer_plugins(self.logger, ""):
                raise TensorRTError("TensorRT plugin initialization failed")
            self.runtime = trt.Runtime(self.logger)
            self.engine = self.runtime.deserialize_cuda_engine(path.read_bytes())
            if self.engine is None:
                raise TensorRTError("Engine deserialization failed; rebuild on this Jetson and TensorRT version")
            if self.engine.num_io_tensors != 2:
                raise TensorRTError("YOLO engine must have exactly one input and one raw output")
            names = [self.engine.get_tensor_name(index) for index in range(self.engine.num_io_tensors)]
            inputs = [name for name in names if self.engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT]
            outputs = [name for name in names if self.engine.get_tensor_mode(name) == trt.TensorIOMode.OUTPUT]
            if len(inputs) != 1 or len(outputs) != 1 or len(set(names)) != 2:
                raise TensorRTError("Invalid YOLO engine input/output configuration")
            self.input_name, self.output_name = inputs[0], outputs[0]
            for name in names:
                if (self.engine.get_tensor_format(name) != trt.TensorFormat.LINEAR
                        or self.engine.get_tensor_location(name) != trt.TensorLocation.DEVICE
                        or self.engine.is_shape_inference_io(name)):
                    raise TensorRTError(f"Unsupported tensor layout/location/shape input: {name}")
            self.input_dtype = np.dtype(trt.nptype(self.engine.get_tensor_dtype(self.input_name)))
            self.output_dtype = np.dtype(trt.nptype(self.engine.get_tensor_dtype(self.output_name)))
            if any(dtype not in (np.dtype("float32"), np.dtype("float16"))
                   for dtype in (self.input_dtype, self.output_dtype)):
                raise TensorRTError("YOLO engine I/O must use FP32 or FP16")
            self.stream = self.cuda.stream()
            self.context = self.engine.create_execution_context()
            if self.context is None:
                raise TensorRTError("TensorRT execution context allocation failed")
            declared_shape = tuple(self.engine.get_tensor_shape(self.input_name))
            if declared_shape != self.input_shape:
                if (len(declared_shape) != 4
                        or any(value != -1 and value != target for value, target in zip(declared_shape, self.input_shape))):
                    raise TensorRTError(f"Expected input shape {INPUT_SHAPE}, got {declared_shape}")
                if not self.context.set_input_shape(self.input_name, self.input_shape):
                    raise TensorRTError("Engine optimization profile does not allow 640x640 input")
            missing = self.context.infer_shapes()
            if missing:
                raise TensorRTError(f"Engine shapes could not be resolved: {missing}")
            if tuple(self.context.get_tensor_shape(self.input_name)) != self.input_shape:
                raise TensorRTError("Engine input shape differs after shape inference")
            if tuple(self.context.get_tensor_shape(self.output_name)) != self.output_shape:
                raise TensorRTError(f"Expected raw YOLO output {self.output_shape}; export the selected model without NMS/objectness")
            for name, shape, dtype in ((self.input_name, self.input_shape, self.input_dtype),
                                       (self.output_name, self.output_shape, self.output_dtype)):
                pointer = self.cuda.malloc(allocation_size(shape, dtype))
                self.device_buffers.append(pointer)
                if not self.context.set_tensor_address(name, pointer):
                    raise TensorRTError(f"TensorRT rejected tensor address: {name}")
            self.host_input, pointer = self.cuda.host_array(self.input_shape, self.input_dtype)
            self.host_buffers.append(pointer)
            self.host_output, pointer = self.cuda.host_array(self.output_shape, self.output_dtype)
            self.host_buffers.append(pointer)
            for _ in range(warmup):
                self.predict(np.zeros(self.input_shape, dtype=np.float32))
        except ImportError as exc:
            with suppress(Exception):
                self.close()
            raise TensorRTError("TensorRT Python bindings unavailable; use JetPack's system Python environment") from exc
        except Exception:
            with suppress(Exception):
                self.close()
            raise

    def predict(self, blob):
        array = np.asarray(blob)
        lower, upper = (-3, 3) if self.score_channels == 0 else (0, 1)
        if (array.shape != self.input_shape or array.dtype not in (np.dtype("float32"), np.dtype("float16"))
                or not np.isfinite(array).all() or array.min() < lower or array.max() > upper):
            raise ValueError("Expected normalized RGB NCHW FP32/FP16 array [1,3,640,640] in [0,1]")
        with self.lock:
            if self.closed:
                raise TensorRTError("TensorRT detector is closed")
            try:
                # asyncio.to_thread may select another host thread between frames.
                self.cuda.set_device(self.device)
                np.copyto(self.host_input, array, casting="same_kind")
                self.cuda.copy(self.device_buffers[0], self.host_input.ctypes.data,
                               self.host_input.nbytes, 1, self.stream)  # HostToDevice
                if not self.context.execute_async_v3(stream_handle=self.stream):
                    raise TensorRTError("TensorRT execute_async_v3 failed")
                self.cuda.copy(self.host_output.ctypes.data, self.device_buffers[1],
                               self.host_output.nbytes, 2, self.stream)  # DeviceToHost
                self.cuda.synchronize(self.stream)
                output = self.host_output.astype(np.float32, copy=True)
                if not np.isfinite(output).all():
                    raise TensorRTError("TensorRT returned non-finite predictions")
                # Pose channels after the person score contain x/y pixel
                # coordinates. They are not COCO class probabilities.
                scores = output[:, 4:4 + self.score_channels, :]
                if self.score_channels and (scores.min() < -0.001 or scores.max() > 1.001):
                    raise TensorRTError("YOLO output class scores must be probabilities in [0,1]")
                return output
            except Exception:
                with suppress(Exception):
                    self.close()
                raise

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            errors = []
            if self.cuda:
                try:
                    self.cuda.set_device(self.device)
                    if self.stream:
                        self.cuda.synchronize(self.stream)
                except Exception as exc:
                    errors.append(exc)
            self.context = self.engine = self.runtime = None
            if self.cuda:
                for pointer in self.device_buffers:
                    try:
                        self.cuda.free(pointer)
                    except Exception as exc:
                        errors.append(exc)
                for pointer in self.host_buffers:
                    try:
                        self.cuda.free_host(pointer)
                    except Exception as exc:
                        errors.append(exc)
                if self.stream:
                    try:
                        self.cuda.destroy_stream(self.stream)
                    except Exception as exc:
                        errors.append(exc)
            self.device_buffers.clear()
            self.host_buffers.clear()
            self.host_input = self.host_output = None
            self.stream = None
            if errors:
                raise TensorRTError(f"CUDA resource cleanup failed: {errors[0]}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", type=Path)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--pose", action="store_true", help="Require raw 17-keypoint pose output [1,56,8400]")
    args = parser.parse_args()
    if not 1 <= args.runs <= 1000:
        parser.error("Runs must be 1..1000")
    try:
        with TensorRTDetector(args.engine,
                output_shape=POSE_OUTPUT_SHAPE if args.pose else OUTPUT_SHAPE,
                score_channels=1 if args.pose else 80) as detector:
            blob = np.zeros(INPUT_SHAPE, dtype=np.float32)
            durations = []
            for _ in range(args.runs):
                started = time.perf_counter()
                result = detector.predict(blob)
                durations.append((time.perf_counter() - started) * 1000)
            print(f"Real TensorRT engine ready: output={result.shape}, input={detector.input_dtype}, "
                  f"median={np.median(durations):.2f}ms, p95={np.percentile(durations, 95):.2f}ms")
            print("Zero-input smoke test only; validate boxes on real camera frames before the demo.")
    except (TensorRTError, ValueError, OSError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
