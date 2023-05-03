import math

import time
import torch
import numpy as np
from loguru import logger

from libs import tt_lib as ttl
from libs.tt_lib.utils import (
    _nearest_32 as nearest_32,
    pad_activation,
    pad_weight,
    tilize,
    tilize_to_list,
    untilize,
    print_diff_argmax,
    tt2torch,
    tt2torch_rm,
    roundup,
    roundup32,
    float_to_bits,
    divup,
    channels_last,
    convert_weights_2d_matrix,
)


def is_close(a, b, rtol=1e-2, atol=1e-2, max_mag=2.0, max_mag_fraction=0.02):
    """
    A variant of np.isclose with logging.
    """
    absdiff = (a - b).abs()
    reldiff1 = (a.abs() / b.abs()) - 1.0
    reldiff2 = (a.abs() + 1.0) / (b.abs() + 1.0) - 1.0  # in case b.abs() is 0
    reldiff_or = torch.logical_or(reldiff1.abs() < rtol, reldiff2.abs() < rtol)
    max_mag_ok = absdiff < max_mag * max_mag_fraction

    or_abs_rel = torch.logical_or(absdiff < atol, reldiff_or)
    or_abs_rel = torch.logical_or(or_abs_rel, max_mag_ok)
    debug_index = or_abs_rel.to(torch.int32).argmin().item()
    if not or_abs_rel.reshape(-1)[debug_index]:
        print("isclose mismatch at index=", debug_index)
        print(a.reshape(-1)[debug_index])
        print(b.reshape(-1)[debug_index])
        print("reldiff1=", reldiff1.reshape(-1)[debug_index])
        print("reldiff2=", reldiff2.reshape(-1)[debug_index])
        print("absdiff=", absdiff.reshape(-1)[debug_index])
        HT = a.shape[-2] // 32
        WT = a.shape[-1] // 32
        hwt = debug_index//1024
        wt = hwt % WT
        ht = hwt // WT
        h = (debug_index % 1024) // 32
        w = (debug_index % 1024) % 32
        print("****    at ", debug_index, " --- ", "HTWT=", ht, wt, "HW=", h, w)
    return torch.all(or_abs_rel)


def print_diff_tt_pyt(a, b, annotation=""):
    # first convert a pytorch tensor argument b to tt
    padded_b = pad_weight(b)
    pyt_a = tt2torch(a)  # untilizes also
    return print_diff_argmax(pyt_a, padded_b, annotation)


def get_oom_of_float(float_lst):
    """
    Given a list of floats, returns a list of the order or magnitudes
    of the floats. Useful when you want to make sure that even if your
    tt outputs don't match pytorch all that well, they are at least
    on the same order of magnitude
    """
    ooms = []
    for el in float_lst:
        str_el = str(el)
        if "e" in str_el:
            oom = int(str_el.split("e")[1])
        elif str_el[:2] == "0.":
            str_el = str_el.split(".")[1]

            oom = -1
            for e in str_el:
                if e != "0":
                    break
                oom -= 1
        else:
            oom = len(str_el.split(".")[0])

        ooms.append(oom)

    return ooms


def ttP(x, count=4, offset=0, stride=1):
    if type(x) == torch.Tensor:
        t1 = x.reshape(-1)
    else:
        host = ttl.device.GetHost()
        shp = x.shape()
        tt_out = x.to(host)
        torch_out = untilize(torch.Tensor(tt_out.data()).reshape(shp))
        t1 = torch_out.reshape(-1)
    print("Tensor vals: (", end="")
    for j in range(offset, offset + count * stride, stride):
        print(t1[j].item(), " ", end="")
    print(")")


def enable_compile_cache():
    """
    Enables persistent compiled kernel caching - disables recompiling the kernels for the duration of running process if built_kernels/.../hash directory with kernel binaries is present.
    """
    ttl.device.EnableCompileCache()


def disable_compile_cache():
    """
    Disables persistent compiled kernel caching. This is the default state.
    """
    ttl.device.DisableCompileCache()


def get_compile_cache_enabled():
    """
    Returns the current state of persistent compile cache on/off switch.
    """
    return ttl.device.GetCompileCacheEnabled()


def comp_allclose(golden, calculated, rtol=1e-05, atol=1e-08):
    if golden.dtype != calculated.dtype:
        calculated = calculated.type(golden.dtype)

    atol_delta = torch.max(torch.abs(golden - calculated)).item()
    rtol_delta = torch.max(
        torch.abs(golden - calculated) / torch.abs(calculated)
    ).item()
    return (
        torch.allclose(golden, calculated, rtol, atol, True),
        f"Max ATOL Delta: {atol_delta}, Max RTOL Delta: {rtol_delta}",
    )


def comp_pcc(golden, calculated, pcc=0.99):
    if golden.dtype != calculated.dtype:
        calculated = calculated.type(golden.dtype)

    if torch.all(torch.isnan(golden)) and torch.all(torch.isnan(calculated)):
        logger.warning("Both tensors are 'nan'")
        return True, f"PCC: {1.0}"

    if torch.all(torch.isnan(golden)) or torch.all(torch.isnan(calculated)):
        logger.error("One tensor is all nan, the other is not.")
        return False, f"PCC: {0.0}"

    # Test if either is completely zero
    if torch.any(golden.bool()) != torch.any(calculated.bool()):
        logger.error("One tensor is all zero")
        return False, f"PCC: {0.0}"

    # if torch.any(torch.isinf(golden)) or torch.any(torch.isinf(calculated)):
    #    raise RuntimeError(f"Tensor overflow to infinity: \n{golden}\n{calculated}")

    # if torch.any(torch.isneginf(golden)) or torch.any(torch.isneginf(calculated)):
    #    raise RuntimeError(f"Tensor overflow to negative infinity: \n{golden}\n{calculated}")

    # For now, mask all infs and nans so that we check the rest... TODO
    golden = golden.clone()
    golden[
        torch.logical_or(
            torch.isnan(golden),
            torch.logical_or(torch.isinf(golden), torch.isneginf(golden)),
        )
    ] = 0
    calculated = calculated.clone()
    calculated[
        torch.logical_or(
            torch.isnan(calculated),
            torch.logical_or(torch.isinf(calculated), torch.isneginf(calculated)),
        )
    ] = 0

    if torch.equal(golden, calculated):
        return True, f"PCC: {1.0}"

    if golden.dtype == torch.bfloat16:
        golden = golden.type(torch.float32)
        calculated = calculated.type(torch.float32)
    cal_pcc = np.min(
        np.ma.corrcoef(
            np.ma.masked_invalid(torch.squeeze(golden).detach().numpy()).flatten(),
            np.ma.masked_invalid(torch.squeeze(calculated).detach().numpy()).flatten(),
        )
    )

    if isinstance(cal_pcc, np.ma.core.MaskedConstant):
        return True, f"PCC: {1.0}"

    return cal_pcc >= pcc, f"PCC: {cal_pcc}"


def comp_allclose_and_pcc(golden, calculated, rtol=1e-05, atol=1e-08, pcc=0.99):
    if golden.dtype != calculated.dtype:
        calculated = calculated.type(golden.dtype)

    passing = True
    output = ""
    passing_allclose, output_allclose = comp_allclose(golden, calculated, rtol, atol)
    passing &= passing_allclose
    output += output_allclose
    if torch.numel(golden) != 1:
        passing_pcc, output_pcc = comp_pcc(golden, calculated, pcc)
        passing &= passing_pcc
        output += f", {output_pcc}"

    return passing, output

def torch2tt_tensor(py_tensor: torch.Tensor, tt_device, tt_layout=ttl.tensor.Layout.TILE, tt_memory_config=ttl.tensor.MemoryConfig(True, -1)):
    size = list(py_tensor.size())

    while len(size) < 4:
        size.insert(0, 1)

    tt_tensor = ttl.tensor.Tensor(
        py_tensor.reshape(-1).tolist(),
        size,
        ttl.tensor.DataType.BFLOAT16,
        ttl.tensor.Layout.ROW_MAJOR,
    ).to(tt_layout).to(tt_device, tt_memory_config)

    return tt_tensor


def tt2torch_tensor(tt_tensor, tt_host=None):
    if tt_host == None:
        host = ttl.device.GetHost()
    else:
        host = tt_host
    tt_output = tt_tensor.to(host).to(ttl.tensor.Layout.ROW_MAJOR)
    py_output = torch.Tensor(tt_output.data()).reshape(tt_output.shape())
    return py_output


class Profiler():
    def __init__(self):
        self.start_times = dict()
        self.times = dict()
        self.disabled = False

    def enable(self):
        self.disabled = False

    def disable(self):
        self.disabled = True

    def start(self, key):
        if self.disabled:
            return

        self.start_times[key] = time.time()

    def end(self, key, PERF_CNT=1):
        if self.disabled:
            return

        if key not in self.start_times:
            return

        diff = time.time() - self.start_times[key]

        if key not in self.times:
            self.times[key] = []

        self.times[key].append(diff / PERF_CNT)

    def get(self, key):
        if key not in self.times:
            return 0

        return sum(self.times[key]) / len(self.times[key])

    def print(self):

        for key in self.times:
            average = self.get(key)
            print(f"{key}: {average:.3f}s")


profiler = Profiler()
def tt_to_torch_tensor(tt_tensor, host):
    tt_tensor = tt_tensor.to(host).to(ttl.tensor.Layout.ROW_MAJOR)
    # create a 1D PyTorch tensor from values in TT Tensor obtained with data() member function
    # and then reshape PyTorch tensor to shape of TT Tensor
    py_tensor = torch.Tensor(tt_tensor.data()).reshape(tt_tensor.shape())
    return py_tensor

def torch_to_tt_tensor_rm(py_tensor, device):
    shape = list(py_tensor.size())
    while len(shape) < 4:
        shape.insert(0, 1)

    tt_tensor = (
        ttl.tensor.Tensor(
            py_tensor.reshape(-1).tolist(), # PyTorch tensor flatten into a list of floats
            shape,               # shape of TT Tensor that will be created
            ttl.tensor.DataType.BFLOAT16, # data type that will be used in created TT Tensor
            ttl.tensor.Layout.ROW_MAJOR,  # memory layout that will be used in created TT Tensor
        )
        .to(device)                         # move TT Tensor from host to TT accelerator device (device is of type tt_lib.device.Device)
    )

    return tt_tensor

def torch_to_tt_tensor(py_tensor, device):
    shape = list(py_tensor.size())
    while len(shape) < 4:
        shape.insert(0, 1)

    tt_tensor = (
        ttl.tensor.Tensor(
            py_tensor.reshape(-1).tolist(), # PyTorch tensor flatten into a list of floats
            shape,               # shape of TT Tensor that will be created
            ttl.tensor.DataType.BFLOAT16, # data type that will be used in created TT Tensor
            ttl.tensor.Layout.ROW_MAJOR,  # memory layout that will be used in created TT Tensor
        )
        .to(ttl.tensor.Layout.TILE)     # change memory layout of TT Tensor to TILE (as operation that will use it expects TILE layout)
        .to(device)                         # move TT Tensor from host to TT accelerator device (device is of type tt_lib.device.Device)
    )

    return tt_tensor

def print_corr_coef(x: torch.Tensor, y: torch.Tensor):
    x = torch.reshape(x, (-1, ))
    y = torch.reshape(y, (-1, ))

    input = torch.stack((x, y))

    corrval = torch.corrcoef(input)
    print(f"Corr coef:")
    print(f"{corrval}")
