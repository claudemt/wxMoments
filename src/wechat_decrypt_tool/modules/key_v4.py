import ctypes
import multiprocessing
import struct
import hmac
import os
from ctypes import wintypes

from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512

try:
    import pymem
except ImportError:
    pymem = None

try:
    import yara
except ImportError:
    yara = None

PROCESS_ALL_ACCESS = 0x1F0FFF
PAGE_READWRITE = 0x04
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000

IV_SIZE = 16
HMAC_SHA256_SIZE = 64
HMAC_SHA512_SIZE = 64
KEY_SIZE = 32
AES_BLOCK_SIZE = 16
ROUND_COUNT = 256000
PAGE_SIZE = 4096
SALT_SIZE = 16


PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

_SALT_XOR_MASK = 0x3a
_MAC_KEY_ITERATIONS = 2
_PAGE_NUMBER = 1
_MAX_PROCESS_SPLITS = 40
_MIN_UNIQUE_BYTES = 15
_MAX_PRINTABLE_COUNT = 24
_VERIFY_CHUNK_SIZE = 16

finish_flag = False
_DEBUG_KEY_SCAN = os.environ.get("WXMOMENTS_DEBUG_KEY_SCAN", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug(message: str) -> None:
    if _DEBUG_KEY_SCAN:
        print(message)


def xor_raw_key(raw_key: bytes, internal_db_key: bytes | None) -> bytes:
    if internal_db_key is None:
        return raw_key
    if len(raw_key) != KEY_SIZE:
        raise ValueError(f"raw key length must be {KEY_SIZE}, got {len(raw_key)}")
    if len(internal_db_key) != KEY_SIZE:
        raise ValueError(f"internal_db_key length must be {KEY_SIZE}, got {len(internal_db_key)}")
    return bytes(a ^ b for a, b in zip(raw_key, internal_db_key))


def verify_worker(task):
    return check_chunk(*task)

if os.name == 'nt':
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    ReadProcessMemory = kernel32.ReadProcessMemory
    ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t,
                                  ctypes.POINTER(ctypes.c_size_t)]
    ReadProcessMemory.restype = wintypes.BOOL

    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL
else:
    kernel32 = None
    OpenProcess = None
    ReadProcessMemory = None
    CloseHandle = None


def _require_windows_runtime():
    if os.name != 'nt':
        raise RuntimeError('V4 数据库密钥提取仅支持 Windows。')
    if pymem is None or yara is None:
        raise RuntimeError('V4 数据库密钥提取缺少 Windows 运行时依赖。')


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]



def open_process(pid):
    return ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)



def read_process_memory(process_handle, address, size):
    buffer = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    success = ctypes.windll.kernel32.ReadProcessMemory(
        process_handle,
        ctypes.c_void_p(address),
        buffer,
        size,
        ctypes.byref(bytes_read)
    )
    if not success:
        return None
    return buffer.raw



def get_memory_regions(process_handle):
    regions = []
    mbi = MEMORY_BASIC_INFORMATION()
    address = 0
    while ctypes.windll.kernel32.VirtualQueryEx(
            process_handle,
            ctypes.c_void_p(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi)
    ):
        if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE:
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        address += mbi.RegionSize
    return regions


def read_num(data: bytes, offset, size):
    if size == 1:
        fmt = '<B'
    elif size == 2:
        fmt = '<H'
    elif size == 4:
        fmt = '<I'
    elif size == 8:
        fmt = '<Q'
    else:
        raise ValueError("Unsupported size")
    return struct.unpack_from(fmt, data, offset)[0]


def read_bytes_from_pid(pid: int, addr: int, size: int):
    hprocess = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not hprocess:
        raise Exception(f"Failed to open process with PID {pid}")
    buffer = b''
    try:
        buffer = ctypes.create_string_buffer(size)
        bytes_read = ctypes.c_size_t(0)
        success = ReadProcessMemory(hprocess, addr, buffer, size, ctypes.byref(bytes_read))
        if not success:
            CloseHandle(hprocess)
            return b''
        CloseHandle(hprocess)
    except Exception:
        pass
    return bytes(buffer)


def is_ok(passphrase, buf, internal_db_key=None):
    global finish_flag
    if finish_flag:
        return False
    salt = buf[:SALT_SIZE]
    mac_salt = bytes(x ^ _SALT_XOR_MASK for x in salt)
    passphrase = xor_raw_key(passphrase, internal_db_key)
    new_key = PBKDF2(passphrase, salt, dkLen=KEY_SIZE, count=ROUND_COUNT, hmac_hash_module=SHA512)
    mac_key = PBKDF2(new_key, mac_salt, dkLen=KEY_SIZE, count=_MAC_KEY_ITERATIONS, hmac_hash_module=SHA512)
    reserve = IV_SIZE + HMAC_SHA512_SIZE
    reserve = ((reserve + AES_BLOCK_SIZE - 1) // AES_BLOCK_SIZE) * AES_BLOCK_SIZE
    start = SALT_SIZE
    end = PAGE_SIZE
    mac = hmac.new(mac_key, buf[start:end - reserve + IV_SIZE], SHA512)
    mac.update(struct.pack('<I', _PAGE_NUMBER))
    hash_mac = mac.digest()
    hash_mac_start_offset = end - reserve + IV_SIZE
    hash_mac_end_offset = hash_mac_start_offset + len(hash_mac)
    if hash_mac == buf[hash_mac_start_offset:hash_mac_end_offset]:
        _debug("[+] Found valid key!")
        finish_flag = True
        return True
    return False


def check_chunk(chunk, buf, internal_db_key=None):
    global finish_flag
    if finish_flag:
        return False
    if is_ok(chunk, buf, internal_db_key):
        return chunk
    return False


def is_potential_key(key: bytes) -> bool:
    if len(key) != KEY_SIZE:
        return False
    if len(set(key)) < _MIN_UNIQUE_BYTES:
        return False
    printable_count = sum(32 <= b <= 126 for b in key)
    if printable_count > _MAX_PRINTABLE_COUNT:
        return False
    return True


def get_key_inner(pid, process_infos):
    _require_windows_runtime()
    process_handle = open_process(pid)
    rules_v4_key = r'''
        rule GetKeyAddrStub
        {
            strings:
                $a = { ?? ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 20 00 00 00 00 00 00 00 2f 00 00 00 00 00 00 00 }
            condition:
                all of them
        }
        '''
    rules = yara.compile(source=rules_v4_key)
    pre_addresses = []

    for base_address, region_size in process_infos:
        memory = read_process_memory(process_handle, base_address, region_size)
        if not memory:
            continue

        matches = rules.match(data=memory)
        if matches:
            for match in matches:
                rule_name = match.rule
                if rule_name == 'GetKeyAddrStub':
                    for string in match.strings:
                        for instance in string.instances:
                            offset, content = instance.offset, instance.matched_data
                            addr = read_num(memory, offset, 8)
                            pre_addresses.append(addr)

    keys = []
    key_set = set()
    for pre_address in pre_addresses:
        key = read_bytes_from_pid(pid, pre_address, KEY_SIZE)
        if key not in key_set:
            keys.append(key)
            key_set.add(key)

    return keys


def get_key(pid, process_handle, buf, internal_db_key=None):
    process_infos = get_memory_regions(process_handle)

    def split_list(lst, n):
        k, m = divmod(len(lst), n)
        return (lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n))

    keys = []
    pool = multiprocessing.Pool(processes=multiprocessing.cpu_count() // 2)
    results = pool.starmap(get_key_inner, ((pid, process_info_) for process_info_ in
                                           split_list(process_infos, min(len(process_infos), _MAX_PROCESS_SPLITS))))
    pool.close()
    pool.join()

    raw_keys = []
    for r in results:
        if r:
            raw_keys += r

    unique_keys = list(set(raw_keys))

    filtered_keys = [k for k in unique_keys if is_potential_key(k)]

    _debug(f"[*] Total raw candidates extracted: {len(unique_keys)}")
    _debug(f"[*] Remaining candidates after entropy/ASCII filtering: {len(filtered_keys)}")

    key = verify_keys(filtered_keys, buf, internal_db_key)
    return key


def verify_keys(keys, buf, internal_db_key=None):
    total = len(keys)
    if total == 0:
        _debug("[-] No key candidates found")
        return None

    worker_count = max(1, multiprocessing.cpu_count() // 2)
    _debug(f"[*] Testing {total} filtered key candidates with {worker_count} workers...")

    completed = 0
    last_percent = -1
    with multiprocessing.Pool(processes=worker_count) as pool:
        task_iter = ((key, buf, internal_db_key) for key in keys)
        for r in pool.imap_unordered(verify_worker, task_iter, chunksize=_VERIFY_CHUNK_SIZE):
            completed += 1
            percent = int((completed / total) * 100)
            if percent != last_percent:
                _debug(f"[*] Verify progress: {completed}/{total} ({percent}%)")
                last_percent = percent

            if r:
                _debug(f"[+] Key found (length={len(r)} bytes; value redacted)")
                pool.terminate()
                return bytes.hex(r)

    _debug("[-] Verification completed, no valid key")
    return None


def recover_key(pid, db_file_path=None, internal_db_key=None):
    try:
        _require_windows_runtime()
    except RuntimeError as exc:
        _debug(f"[-] {exc}")
        return None

    process_handle = open_process(pid)
    if not process_handle:
        _debug(f"[-] Failed to open process {pid}")
        return None

    if not db_file_path:
        _debug("[-] No database file specified")
        CloseHandle(process_handle)
        return None

    if not os.path.exists(db_file_path):
        _debug(f"[-] Database file not found: {db_file_path}")
        CloseHandle(process_handle)
        return None

    try:
        with open(db_file_path, 'rb') as f:
            buf = f.read()

        if len(buf) < PAGE_SIZE:
            _debug(f"[-] Database file too small: {len(buf)} bytes")
            CloseHandle(process_handle)
            return None

        _debug("[*] Scanning process memory for key candidates...")
        key = get_key(pid, process_handle, buf, internal_db_key)

        CloseHandle(process_handle)
        return key

    except Exception as e:
        _debug(f"[-] Error during key recovery: {e}")
        CloseHandle(process_handle)
        return None
