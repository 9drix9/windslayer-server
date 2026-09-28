"""
Windows debugger for WindSlayer_patched.exe using Debug API.

Sets INT3 breakpoints at key locations and logs hits.
Strategy:
  1. Breakpoint at 0x44D65C (packet dispatch jump) - logs every opcode received
  2. Breakpoint at 0x450797 (end of 0x2B handler's call to 0x443280) - verifies 0x2B ran
  3. Breakpoint at 0x489DA0 (dialog add function) - logs every dialog shown/dismissed
"""
import ctypes
import ctypes.wintypes as wt
import struct
import time
import threading
import sys

kernel32 = ctypes.windll.kernel32

# --- Constants ---
DEBUG_ONLY_THIS_PROCESS = 0x00000002
DEBUG_PROCESS           = 0x00000001
PROCESS_ALL_ACCESS      = 0x1F0FFF
INFINITE                = 0xFFFFFFFF

# Debug events
EXCEPTION_DEBUG_EVENT      = 1
CREATE_THREAD_DEBUG_EVENT  = 2
CREATE_PROCESS_DEBUG_EVENT = 3
EXIT_THREAD_DEBUG_EVENT    = 4
EXIT_PROCESS_DEBUG_EVENT   = 5
LOAD_DLL_DEBUG_EVENT       = 6
UNLOAD_DLL_DEBUG_EVENT     = 7
OUTPUT_DEBUG_STRING_EVENT  = 8
RIP_EVENT                  = 9

EXCEPTION_BREAKPOINT       = 0x80000003
EXCEPTION_SINGLE_STEP      = 0x80000004
# WoW64 (32-bit process on 64-bit Windows) uses different codes
STATUS_WX86_BREAKPOINT     = 0x4000001F
STATUS_WX86_SINGLE_STEP    = 0x4000001E

DBG_CONTINUE               = 0x00010002
DBG_EXCEPTION_NOT_HANDLED  = 0x80010001

CONTEXT_FULL              = 0x10007
CONTEXT_DEBUG_REGISTERS   = 0x10010

TH32CS_SNAPPROCESS         = 0x00000002

# --- Structures ---
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wt.DWORD), ('cntUsage', wt.DWORD),
        ('th32ProcessID', wt.DWORD), ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wt.DWORD), ('cntThreads', wt.DWORD),
        ('th32ParentProcessID', wt.DWORD), ('pcPriClassBase', wt.LONG),
        ('dwFlags', wt.DWORD), ('szExeFile', wt.WCHAR * 260),
    ]

class FLOATING_SAVE_AREA(ctypes.Structure):
    _fields_ = [
        ('ControlWord', wt.DWORD), ('StatusWord', wt.DWORD),
        ('TagWord', wt.DWORD), ('ErrorOffset', wt.DWORD),
        ('ErrorSelector', wt.DWORD), ('DataOffset', wt.DWORD),
        ('DataSelector', wt.DWORD), ('RegisterArea', ctypes.c_byte * 80),
        ('Cr0NpxState', wt.DWORD),
    ]

class CONTEXT32(ctypes.Structure):
    _fields_ = [
        ('ContextFlags', wt.DWORD),
        ('Dr0', wt.DWORD), ('Dr1', wt.DWORD), ('Dr2', wt.DWORD),
        ('Dr3', wt.DWORD), ('Dr6', wt.DWORD), ('Dr7', wt.DWORD),
        ('FloatSave', FLOATING_SAVE_AREA),
        ('SegGs', wt.DWORD), ('SegFs', wt.DWORD),
        ('SegEs', wt.DWORD), ('SegDs', wt.DWORD),
        ('Edi', wt.DWORD), ('Esi', wt.DWORD), ('Ebx', wt.DWORD), ('Edx', wt.DWORD),
        ('Ecx', wt.DWORD), ('Eax', wt.DWORD),
        ('Ebp', wt.DWORD), ('Eip', wt.DWORD),
        ('SegCs', wt.DWORD), ('EFlags', wt.DWORD),
        ('Esp', wt.DWORD), ('SegSs', wt.DWORD),
        ('ExtendedRegisters', ctypes.c_byte * 512),
    ]

class EXCEPTION_RECORD(ctypes.Structure):
    pass
EXCEPTION_RECORD._fields_ = [
    ('ExceptionCode', wt.DWORD),
    ('ExceptionFlags', wt.DWORD),
    ('ExceptionRecord', ctypes.POINTER(EXCEPTION_RECORD)),
    ('ExceptionAddress', ctypes.c_void_p),
    ('NumberParameters', wt.DWORD),
    ('ExceptionInformation', ctypes.c_void_p * 15),
]

class EXCEPTION_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('ExceptionRecord', EXCEPTION_RECORD), ('dwFirstChance', wt.DWORD)]

class CREATE_PROCESS_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('hFile', ctypes.c_void_p), ('hProcess', ctypes.c_void_p),
                ('hThread', ctypes.c_void_p), ('lpBaseOfImage', ctypes.c_void_p),
                ('dwDebugInfoFileOffset', wt.DWORD), ('nDebugInfoSize', wt.DWORD),
                ('lpThreadLocalBase', ctypes.c_void_p), ('lpStartAddress', ctypes.c_void_p),
                ('lpImageName', ctypes.c_void_p), ('fUnicode', wt.WORD)]

class CREATE_THREAD_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('hThread', ctypes.c_void_p),
                ('lpThreadLocalBase', ctypes.c_void_p),
                ('lpStartAddress', ctypes.c_void_p)]

class LOAD_DLL_DEBUG_INFO(ctypes.Structure):
    _fields_ = [('hFile', ctypes.c_void_p), ('lpBaseOfDll', ctypes.c_void_p),
                ('dwDebugInfoFileOffset', wt.DWORD), ('nDebugInfoSize', wt.DWORD),
                ('lpImageName', ctypes.c_void_p), ('fUnicode', wt.WORD)]

class U(ctypes.Union):
    _fields_ = [('Exception', EXCEPTION_DEBUG_INFO),
                ('CreateThread', CREATE_THREAD_DEBUG_INFO),
                ('CreateProcessInfo', CREATE_PROCESS_DEBUG_INFO),
                ('LoadDll', LOAD_DLL_DEBUG_INFO),
                ('_pad', ctypes.c_byte * 164)]

class DEBUG_EVENT(ctypes.Structure):
    _fields_ = [('dwDebugEventCode', wt.DWORD),
                ('dwProcessId', wt.DWORD),
                ('dwThreadId', wt.DWORD),
                ('u', U)]

# --- Helpers ---
def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    e = PROCESSENTRY32W(); e.dwSize = ctypes.sizeof(e)
    if not kernel32.Process32FirstW(snap, ctypes.byref(e)):
        kernel32.CloseHandle(snap); return None
    while True:
        if e.szExeFile.lower() == name.lower():
            kernel32.CloseHandle(snap); return e.th32ProcessID
        if not kernel32.Process32NextW(snap, ctypes.byref(e)):
            kernel32.CloseHandle(snap); return None

def read_byte(h, addr):
    b = (ctypes.c_ubyte * 1)(); r = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), ctypes.byref(b), 1, ctypes.byref(r)):
        return b[0]
    return None

def write_byte(h, addr, val):
    b = (ctypes.c_ubyte * 1)(val); w = ctypes.c_size_t(0)
    return kernel32.WriteProcessMemory(h, ctypes.c_void_p(addr), ctypes.byref(b), 1, ctypes.byref(w))

def read_dword(h, addr):
    b = (ctypes.c_ubyte * 4)(); r = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_void_p(addr), ctypes.byref(b), 4, ctypes.byref(r)):
        return struct.unpack('<I', bytes(b))[0]
    return None

def get_context(hThread):
    ctx = CONTEXT32()
    ctx.ContextFlags = CONTEXT_FULL | CONTEXT_DEBUG_REGISTERS
    # Use Wow64GetThreadContext for 32-bit processes on 64-bit Windows
    if kernel32.Wow64GetThreadContext(hThread, ctypes.byref(ctx)):
        return ctx
    if kernel32.GetThreadContext(hThread, ctypes.byref(ctx)):
        return ctx
    return None

def set_context(hThread, ctx):
    if kernel32.Wow64SetThreadContext(hThread, ctypes.byref(ctx)):
        return True
    return kernel32.SetThreadContext(hThread, ctypes.byref(ctx))

# --- Main debugger ---
class Debugger:
    def __init__(self, pid):
        self.pid = pid
        self.hProcess = None
        self.threads = {}  # tid -> hThread
        self.breakpoints = {}  # addr -> (original_byte, label, callback)
        self.single_step_bp = None  # addr to re-arm after single-step
        self.log = open('debugger.log', 'w')

    def logp(self, msg):
        t = time.strftime('%H:%M:%S')
        line = f'{t} {msg}'
        print(line)
        self.log.write(line + '\n')
        self.log.flush()

    def set_bp(self, addr, label='', callback=None):
        orig = read_byte(self.hProcess, addr)
        if orig is None:
            self.logp(f'[!] Failed to read byte at 0x{addr:08X}')
            return False
        write_byte(self.hProcess, addr, 0xCC)
        self.breakpoints[addr] = (orig, label, callback)
        self.logp(f'[+] Breakpoint set at 0x{addr:08X} ({label}), orig byte=0x{orig:02X}')
        return True

    def del_bp(self, addr):
        if addr in self.breakpoints:
            orig, _, _ = self.breakpoints[addr]
            write_byte(self.hProcess, addr, orig)
            del self.breakpoints[addr]

    def attach(self):
        if not kernel32.DebugActiveProcess(self.pid):
            self.logp(f'[!] DebugActiveProcess failed: {kernel32.GetLastError()}')
            return False
        self.logp(f'[+] Attached to PID {self.pid}')
        return True

    def detach(self):
        # Restore all breakpoints
        for addr in list(self.breakpoints):
            self.del_bp(addr)
        kernel32.DebugActiveProcessStop(self.pid)
        self.logp('[+] Detached')

    def event_loop(self):
        evt = DEBUG_EVENT()
        while True:
            if not kernel32.WaitForDebugEvent(ctypes.byref(evt), 100):
                err = kernel32.GetLastError()
                if err == 121:  # timeout
                    continue
                self.logp(f'[!] WaitForDebugEvent err {err}')
                break

            cont = DBG_CONTINUE
            try:
                cont = self.handle_event(evt)
            except Exception as e:
                self.logp(f'[!] Exception in handler: {e}')

            kernel32.ContinueDebugEvent(evt.dwProcessId, evt.dwThreadId, cont)
            if evt.dwDebugEventCode == EXIT_PROCESS_DEBUG_EVENT:
                self.logp('[+] Process exited')
                break

    def handle_event(self, evt):
        code = evt.dwDebugEventCode

        if code == CREATE_PROCESS_DEBUG_EVENT:
            self.hProcess = evt.u.CreateProcessInfo.hProcess
            self.threads[evt.dwThreadId] = evt.u.CreateProcessInfo.hThread
            self.logp(f'[+] Process created, base=0x{evt.u.CreateProcessInfo.lpBaseOfImage or 0:08X}')
            self.on_process_ready()
            return DBG_CONTINUE

        if code == CREATE_THREAD_DEBUG_EVENT:
            self.threads[evt.dwThreadId] = evt.u.CreateThread.hThread
            return DBG_CONTINUE

        if code == EXIT_THREAD_DEBUG_EVENT:
            if evt.dwThreadId in self.threads:
                del self.threads[evt.dwThreadId]
            return DBG_CONTINUE

        if code == LOAD_DLL_DEBUG_EVENT:
            return DBG_CONTINUE

        if code == UNLOAD_DLL_DEBUG_EVENT:
            return DBG_CONTINUE

        if code == OUTPUT_DEBUG_STRING_EVENT:
            return DBG_CONTINUE

        if code == EXCEPTION_DEBUG_EVENT:
            return self.handle_exception(evt)

        if code == EXIT_PROCESS_DEBUG_EVENT:
            return DBG_CONTINUE

        return DBG_CONTINUE

    def on_process_ready(self):
        # Override in subclass
        pass

    def handle_exception(self, evt):
        excep_code = evt.u.Exception.ExceptionRecord.ExceptionCode
        excep_addr = evt.u.Exception.ExceptionRecord.ExceptionAddress or 0
        first_chance = evt.u.Exception.dwFirstChance

        if excep_code in (EXCEPTION_BREAKPOINT, STATUS_WX86_BREAKPOINT):
            if excep_addr in self.breakpoints:
                return self.handle_bp_hit(evt, excep_addr)
            # Unknown BP - pass through
            return DBG_EXCEPTION_NOT_HANDLED

        if excep_code in (EXCEPTION_SINGLE_STEP, STATUS_WX86_SINGLE_STEP):
            return self.handle_single_step(evt)

        # Other exceptions - pass through
        if first_chance:
            return DBG_EXCEPTION_NOT_HANDLED
        self.logp(f'[!] Unhandled exception 0x{excep_code:08X} at 0x{excep_addr:08X}')
        return DBG_EXCEPTION_NOT_HANDLED

    def handle_bp_hit(self, evt, addr):
        tid = evt.dwThreadId
        hThread = self.threads.get(tid)
        if not hThread:
            return DBG_EXCEPTION_NOT_HANDLED

        orig, label, callback = self.breakpoints[addr]
        ctx = get_context(hThread)
        if ctx is None:
            return DBG_EXCEPTION_NOT_HANDLED

        # Call user callback
        if callback:
            try:
                callback(self, ctx, tid)
            except Exception as e:
                self.logp(f'[!] cb err: {e}')

        # Restore original byte
        write_byte(self.hProcess, addr, orig)
        # Back up EIP (EIP points after INT3)
        ctx.Eip -= 1
        # Enable single-step (TF flag)
        ctx.EFlags |= 0x100
        set_context(hThread, ctx)
        # Remember to re-arm after single-step
        self.single_step_bp = addr

        return DBG_CONTINUE

    def handle_single_step(self, evt):
        if self.single_step_bp:
            # Re-arm the breakpoint
            write_byte(self.hProcess, self.single_step_bp, 0xCC)
            self.single_step_bp = None
        return DBG_CONTINUE


# --- User debugger ---
class WSDebugger(Debugger):
    def on_process_ready(self):
        self.set_bp(0x44D65C, 'DISPATCH', self.on_dispatch)
        self.set_bp(0x44F762, 'H_ENTRY', self.on_h1c)
        self.set_bp(0x44FCC1, 'TRANSITION_START', self.on_transition)
        # Before Send of opcode 0x69 that creates list entry 0x295
        self.set_bp(0x468581, 'PRE_SEND_69', self.on_pre_send_69)
        self.set_bp(0x4685B2, 'SEND_69', self.on_send_69)
        self.logp('[+] All breakpoints armed. Play the game.')

    def on_pre_send_69(self, dbg, ctx, tid):
        # esi points to some object used to build the packet
        self.logp(f'[PRE_SEND_69] esi=0x{ctx.Esi:08X} edi=0x{ctx.Edi:08X} ebx=0x{ctx.Ebx:08X}')
        # [edi+0x14] is the socket we'll send on
        sock_at_edi14 = read_dword(dbg.hProcess, ctx.Edi + 0x14)
        self.logp(f'[PRE_SEND_69] socket [edi+0x14] = 0x{sock_at_edi14 or 0:08X}')
        # Compare to known sockets
        p = read_dword(dbg.hProcess, 0x70e6f4)
        s = read_dword(dbg.hProcess, 0x70e6f8)
        t = read_dword(dbg.hProcess, 0x70e6fc)
        self.logp(f'[PRE_SEND_69]   primary=0x{p or 0:08X}')
        self.logp(f'[PRE_SEND_69]   secondary=0x{s or 0:08X}')
        self.logp(f'[PRE_SEND_69]   tertiary=0x{t or 0:08X}')

    def on_send_69(self, dbg, ctx, tid):
        self.logp(f'[SEND_69] about to call Send(1) on socket ecx=0x{ctx.Ecx:08X}')

    def on_opcode_check(self, dbg, ctx, tid):
        # Read byte at [esp+0x43] to see what opcode reached here
        byte_val = rd_u8 = None
        raw = (ctypes.c_ubyte * 1)()
        r = ctypes.c_size_t(0)
        kernel32.ReadProcessMemory(dbg.hProcess, ctypes.c_void_p(ctx.Esp + 0x43),
                                   ctypes.byref(raw), 1, ctypes.byref(r))
        if r.value == 1:
            self.logp(f'[OPCODE_CHECK] [esp+0x43] = 0x{raw[0]:02X} (need 0x2E for transition)')

    def on_transition(self, dbg, ctx, tid):
        self.logp(f'[TRANSITION_START] Scene transition helpers about to run!')

    def on_h1c(self, dbg, ctx, tid):
        # Read opcode from [esp+0x43] in the dispatcher frame
        self.logp(f'[H1C_ENTRY] eax={ctx.Eax:08X} esi={ctx.Esi:08X}')

    def on_2b_entry(self, dbg, ctx, tid):
        self.logp(f'[2B_HANDLER_ENTRY] esi={ctx.Esi:08X} eip={ctx.Eip:08X}')

    def on_2b_helper1(self, dbg, ctx, tid):
        self.logp(f'[2B_CALL_442EE0] reached pre-1st-helper esi={ctx.Esi:08X}')

    def on_2b_helper2(self, dbg, ctx, tid):
        self.logp(f'[2B_CALL_443280] reached pre-2nd-helper esi={ctx.Esi:08X}')
        # Read what's at [esi + 0x4CC] to see if session is set up
        v4cc = read_dword(dbg.hProcess, ctx.Esi + 0x4CC)
        self.logp(f'  [esi+0x4CC] = 0x{v4cc or 0:08X}')

    def on_dispatch(self, dbg, ctx, tid):
        # eax = opcode - 1 (before the jmp)
        opcode = (ctx.Eax + 1) & 0xFF
        self.logp(f'[DISPATCH] opcode=0x{opcode:02X} edx=0x{ctx.Edx:X} esi=0x{ctx.Esi:08X}')

    def on_2b_helper(self, dbg, ctx, tid):
        self.logp(f'[2B_CALL_443280] Reached! esi=0x{ctx.Esi:08X} esp=0x{ctx.Esp:08X}')
        # Read what's at [esi + 0x4CC]
        v4cc = read_dword(dbg.hProcess, ctx.Esi + 0x4CC)
        self.logp(f'  [esi+0x4CC] = 0x{v4cc or 0:08X}')

    def on_timer_no_resp(self, dbg, ctx, tid):
        self.logp(f'[TIMER_NO_RESP] "No response" path hit! ebp=0x{ctx.Ebp:08X}')
        # Read ebp+0xc (edi) and ebp+0x14 (the state)
        edi_arg = read_dword(dbg.hProcess, ctx.Ebp + 0xC)
        lparam  = read_dword(dbg.hProcess, ctx.Ebp + 0x14)
        self.logp(f'  [ebp+0xC] (edi arg) = 0x{edi_arg or 0:X}')
        self.logp(f'  [ebp+0x14] (lparam) = 0x{lparam or 0:X}')

    def on_dialog(self, dbg, ctx, tid):
        # Dialog add: string ptr is at [esp+4] (arg 1)
        arg1 = read_dword(dbg.hProcess, ctx.Esp + 4)
        if arg1:
            # Read the string
            buf = (ctypes.c_ubyte * 64)()
            r = ctypes.c_size_t(0)
            kernel32.ReadProcessMemory(dbg.hProcess, ctypes.c_void_p(arg1),
                                       ctypes.byref(buf), 64, ctypes.byref(r))
            s = bytes(buf).split(b'\x00')[0]
            try: sdecode = s.decode('ascii')
            except: sdecode = repr(s)
            self.logp(f'[DIALOG] add "{sdecode[:50]}"')


def main():
    pid = find_pid('WindSlayer_patched.exe')
    if not pid:
        print('ERROR: WindSlayer_patched.exe not running. Launch it first.')
        return
    print(f'PID: {pid}')
    dbg = WSDebugger(pid)
    if not dbg.attach():
        return
    try:
        dbg.event_loop()
    except KeyboardInterrupt:
        print('\nDetaching...')
    finally:
        dbg.detach()

if __name__ == '__main__':
    main()
