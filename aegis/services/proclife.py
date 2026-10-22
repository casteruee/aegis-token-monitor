"""Windows 进程生命周期保护（UI_NEXT_VERSION_DESIGN §13 验收点）。

实现：把**本进程**加入一个私有 Job Object（``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``）。
效果：本应用（GUI 或其采集线程）通过 ``subprocess`` 启动的所有后代进程
（如 CodexBar 的 ``codexbar-cli.exe``）在本进程退出时被系统**一并回收**，
不留孤儿 —— 且**只**作用于本应用自己的后代，绝不会波及用户独立运行的
同名进程。

设计偏离记录（相对原文档「每家一个有界 multiprocessing 子进程」方案）：
v0.2 实测发现 reader 内部已有硬超时（Codex subprocess 60s、HTTP socket
10s），进程边界带来的唯一真实风险是「应用退出时 CodexBar 孙进程残留」，
Job Object 在不引入 spawn 复杂度的前提下恰好解决该风险。验证证据见
``tests/test_proclife.py`` 与 IMPLEMENTATION_REPORT。

全部 ctypes 实现，标准库；ctypes 调用失败时返回 False（保护不生效），
调用方照常运行 —— Job 是保险层，不是功能依赖。
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

_JOB_HANDLE = None

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def install_kill_on_close() -> bool:
    """把当前进程加入 kill-on-close 私有 Job。返回是否成功（幂等）。"""
    global _JOB_HANDLE
    if sys.platform != "win32":
        return False
    if _JOB_HANDLE is not None:
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        # 显式句柄类型：缺省 int 转换会截断 64 位句柄（实测导致 ERROR_INVALID_HANDLE）
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
        ]
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("proclife: CreateJobObjectW failed")
            return False
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ret = kernel32.SetInformationJobObject(
            handle,
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ret:
            logger.warning("proclife: SetInformationJobObject failed")
            kernel32.CloseHandle(handle)
            return False
        # 本进程加入 job（子进程默认继承 job 成员资格）
        ret = kernel32.AssignProcessToJobObject(
            handle, kernel32.GetCurrentProcess()
        )
        if not ret:
            logger.warning("proclife: AssignProcessToJobObject failed")
            kernel32.CloseHandle(handle)
            return False
        _JOB_HANDLE = handle
        return True
    except Exception as e:  # noqa: BLE001 - 保护层失败不能拖垮主功能
        logger.warning("proclife: install failed: %s", type(e).__name__)
        return False
