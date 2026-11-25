import ctypes as c
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as w
from pathlib import Path
from typing import Any, Iterator

_win_dll = getattr(c, "WinDLL")
_get_last_error = getattr(c, "get_last_error")
_kernel: c.CDLL = _win_dll("kernel32", use_last_error=True)
_advapi: c.CDLL = _win_dll("advapi32", use_last_error=True)


def _bind(library: c.CDLL, name: str, result: Any, *arguments: Any) -> Any:
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


_PVOID = c.c_void_p
_DWORD_PTR = c.POINTER(w.DWORD)
_POINTER = c.POINTER(_PVOID)
_create_file = _bind(
    _kernel,
    "CreateFileW",
    w.HANDLE,
    w.LPCWSTR,
    w.DWORD,
    w.DWORD,
    _PVOID,
    w.DWORD,
    w.DWORD,
    w.HANDLE,
)
_close = _bind(_kernel, "CloseHandle", w.BOOL, w.HANDLE)
_local_free = _bind(_kernel, "LocalFree", w.HLOCAL, w.HLOCAL)
_final_name = _bind(
    _kernel, "GetFinalPathNameByHandleW", w.DWORD, w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD
)
_current_process = _bind(_kernel, "GetCurrentProcess", w.HANDLE)
_drive_type = _bind(_kernel, "GetDriveTypeW", w.UINT, w.LPCWSTR)
_volume = _bind(
    _kernel,
    "GetVolumeInformationByHandleW",
    w.BOOL,
    w.HANDLE,
    w.LPWSTR,
    w.DWORD,
    _DWORD_PTR,
    _DWORD_PTR,
    _DWORD_PTR,
    w.LPWSTR,
    w.DWORD,
)
_open_token = _bind(_advapi, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, _POINTER)
_token_info = _bind(
    _advapi,
    "GetTokenInformation",
    w.BOOL,
    w.HANDLE,
    w.DWORD,
    _PVOID,
    w.DWORD,
    _DWORD_PTR,
)
_sid_string = _bind(_advapi, "ConvertSidToStringSidW", w.BOOL, _PVOID, _POINTER)
_security = _bind(
    _advapi,
    "GetSecurityInfo",
    w.DWORD,
    w.HANDLE,
    w.DWORD,
    w.DWORD,
    _POINTER,
    _POINTER,
    _POINTER,
    _POINTER,
    _POINTER,
)
_get_ace = _bind(_advapi, "GetAce", w.BOOL, _PVOID, w.DWORD, _POINTER)
_control = _bind(
    _advapi,
    "GetSecurityDescriptorControl",
    w.BOOL,
    _PVOID,
    c.POINTER(w.WORD),
    _DWORD_PTR,
)


class _FileInfo(c.Structure):
    _fields_ = [
        ("attributes", w.DWORD),
        ("created", w.FILETIME),
        ("accessed", w.FILETIME),
        ("written", w.FILETIME),
        ("serial", w.DWORD),
        ("size_hi", w.DWORD),
        ("size_lo", w.DWORD),
        ("links", w.DWORD),
        ("index_hi", w.DWORD),
        ("index_lo", w.DWORD),
    ]


class _Acl(c.Structure):
    _fields_ = [
        ("revision", w.BYTE),
        ("reserved", w.BYTE),
        ("size", w.WORD),
        ("count", w.WORD),
        ("reserved2", w.WORD),
    ]


class _Ace(c.Structure):
    _fields_ = [
        ("kind", w.BYTE),
        ("flags", w.BYTE),
        ("size", w.WORD),
        ("mask", w.DWORD),
    ]


class _Overlapped(c.Structure):
    _fields_ = [
        ("internal", c.c_size_t),
        ("internal_high", c.c_size_t),
        ("offset", w.DWORD),
        ("offset_high", w.DWORD),
        ("event", w.HANDLE),
    ]


_info = _bind(
    _kernel, "GetFileInformationByHandle", w.BOOL, w.HANDLE, c.POINTER(_FileInfo)
)
_lock = _bind(
    _kernel,
    "LockFileEx",
    w.BOOL,
    w.HANDLE,
    w.DWORD,
    w.DWORD,
    w.DWORD,
    w.DWORD,
    c.POINTER(_Overlapped),
)


def _sid(pointer: int) -> str:
    value = _PVOID()
    if not _sid_string(pointer, c.byref(value)):
        raise PermissionError
    try:
        return c.wstring_at(value)
    finally:
        _local_free(value)


def _operator_sid() -> str:
    token = _PVOID()
    if not _open_token(_current_process(), 0x0008, c.byref(token)):  # TOKEN_QUERY
        raise PermissionError
    try:
        size = w.DWORD()
        _token_info(token, 1, None, 0, c.byref(size))  # TokenUser
        buffer = c.create_string_buffer(size.value)
        if not size.value or not _token_info(token, 1, buffer, size, c.byref(size)):
            raise PermissionError
        return _sid(c.cast(buffer, _POINTER)[0])
    finally:
        _close(token)


def _check_acl(
    handle: int, operator: str, *, private: bool, root: bool = False
) -> None:
    owner, acl, descriptor = _PVOID(), _PVOID(), _PVOID()
    if _security(
        handle, 1, 5, c.byref(owner), None, c.byref(acl), None, c.byref(descriptor)
    ):
        raise PermissionError
    try:
        trusted = {operator, "S-1-5-18", "S-1-5-32-544"}  # SYSTEM and Administrators
        # Windows servicing can own system ancestors.
        windows_servicing = (
            "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
        )
        owner_address, acl_address = owner.value, acl.value
        if (
            owner_address is None
            or acl_address is None
            or _sid(owner_address)
            not in ({operator} if private else trusted | {windows_servicing})
        ):
            raise PermissionError
        if root:
            control, revision = w.WORD(), w.DWORD()
            if not _control(descriptor, c.byref(control), c.byref(revision)):
                raise PermissionError
            if not control.value & 0x1000:  # SE_DACL_PROTECTED
                raise PermissionError
        for index in range(_Acl.from_address(acl_address).count):
            pointer = _PVOID()
            if not _get_ace(acl, index, c.byref(pointer)) or pointer.value is None:
                raise PermissionError
            ace = _Ace.from_address(pointer.value)
            if ace.kind == 1:  # ACCESS_DENIED_ACE_TYPE
                continue
            if not private and ace.flags & 0x08:  # INHERIT_ONLY_ACE
                continue
            if ace.kind != 0 or ace.size < 16:
                raise PermissionError
            identity = _sid(pointer.value + c.sizeof(_Ace))
            # On ancestors reject untrusted deletion/replacement and ACL/owner changes.
            replacement = 0x100D0040
            if identity == "S-1-3-4":  # OWNER RIGHTS resolves to the validated owner
                continue
            if identity not in trusted and (private or ace.mask & replacement):
                if not (not private and identity == windows_servicing):
                    raise PermissionError
    finally:
        _local_free(descriptor)


@contextmanager
def _open(
    path: Path, *, directory: bool, create: bool = False
) -> Iterator[tuple[int, _FileInfo]]:
    access = 0x20000 | 0x80  # READ_CONTROL | FILE_READ_ATTRIBUTES
    if not directory:
        access |= 0xC0000000  # GENERIC_READ | GENERIC_WRITE for LockFileEx
    flags = 0x00200000 | (
        0x02000000 if directory else 0
    )  # NO reparse follow, backup semantics
    handle = _create_file(str(path), access, 3, None, 4 if create else 3, flags, None)
    if handle == _PVOID(-1).value:
        if _get_last_error() in {2, 3}:
            raise FileNotFoundError
        raise PermissionError
    try:
        info = _FileInfo()
        name = c.create_unicode_buffer(32768)
        length = _final_name(handle, name, len(name), 0)
        if (
            not _info(handle, c.byref(info))
            or not 0 < length < len(name)
            or name.value != "\\\\?\\" + str(path)
            or info.attributes & 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
            or bool(info.attributes & 0x10) != directory
            or (not directory and info.links != 1)
        ):
            raise PermissionError
        yield handle, info
    finally:
        _close(handle)


@contextmanager
def directory(path: Path) -> Iterator[int]:
    if (
        len(path.anchor) != 3
        or path.anchor[1:] != ":\\"
        or _drive_type(path.anchor) != 3
    ):
        raise PermissionError
    operator = _operator_sid()
    with ExitStack() as stack:
        for component in (*reversed(path.parents), path):
            handle, info = stack.enter_context(_open(component, directory=True))
            if component != path:
                _check_acl(handle, operator, private=False)
        fs = c.create_unicode_buffer(32)
        if (
            not _volume(handle, None, 0, None, None, None, fs, len(fs))
            or fs.value != "NTFS"
        ):
            raise PermissionError
        if info.attributes & 0x00441000:  # Offline, recall on open or recall on access
            raise PermissionError
        _check_acl(handle, operator, private=True, root=True)
        yield info.serial


@contextmanager
def file_handle(serial: int, path: Path, *, create: bool = False) -> Iterator[int]:
    with _open(path, directory=False, create=create) as (handle, info):
        if info.serial != serial:
            raise PermissionError
        _check_acl(handle, _operator_sid(), private=True)
        yield handle


def lock(handle: int) -> bool:
    if _lock(handle, 3, 0, 1, 0, c.byref(_Overlapped())):
        return True
    if _get_last_error() == 33:  # ERROR_LOCK_VIOLATION
        return False
    raise PermissionError
