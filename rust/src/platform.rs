//! What rex asks of the operating system beyond `std`: local time, the process's CPU time
//! and the host's load, the host name, a thread's id as Python reports it, pausing and
//! killing a child, the GDM socket, random bytes, and the manager's process groups and
//! signals (Unix only).
//!
//! rex was measured on Unix and the Unix half is that code, moved here unchanged. The
//! Windows half lets the server run there; it declares the few Win32 calls it needs itself
//! rather than adding a crate.

use std::io;
use std::net::UdpSocket;

/// `time.localtime(timestamp)`.
pub(crate) fn local_tm(timestamp: i64) -> Option<libc::tm> {
    // SAFETY: both calls write only into the struct they are given.
    unsafe {
        let mut out: libc::tm = std::mem::zeroed();
        let seconds = timestamp as libc::time_t;
        #[cfg(unix)]
        let ok = !libc::localtime_r(&seconds, &mut out).is_null();
        #[cfg(windows)]
        let ok = libc::localtime_s(&mut out, &seconds) == 0;
        ok.then_some(out)
    }
}

/// `time.mktime`: the epoch of a broken-down local time. `tm_isdst` -1 lets the C library
/// decide whether daylight saving applies.
pub(crate) fn local_mktime(tm: &mut libc::tm) -> i64 {
    // SAFETY: mktime only normalises the struct it is given.
    unsafe {
        #[cfg(unix)]
        return libc::mktime(tm) as i64;
        #[cfg(windows)]
        return win::_mktime64(tm);
    }
}

/// `threading.get_ident()`: `pthread_self()` on Unix, the thread id on Windows.
pub(crate) fn thread_ident() -> u64 {
    // SAFETY: neither call has preconditions.
    unsafe {
        #[cfg(unix)]
        return libc::pthread_self() as u64;
        #[cfg(windows)]
        return u64::from(win::GetCurrentThreadId());
    }
}

/// `socket.gethostname()`; `None` when the system will not say.
pub(crate) fn host_name() -> Option<String> {
    #[cfg(unix)]
    {
        let mut buffer = [0_u8; 256];
        // SAFETY: the buffer is valid for its whole length and gethostname NUL-terminates
        // within it on success.
        if unsafe { libc::gethostname(buffer.as_mut_ptr().cast(), buffer.len()) } != 0 {
            return None;
        }
        let end = buffer.iter().position(|byte| *byte == 0).unwrap_or(buffer.len());
        Some(String::from_utf8_lossy(&buffer[..end]).into_owned())
    }
    // The DNS host name, as CPython's socket.gethostname() reads it. COMPUTERNAME is the NetBIOS
    // name, upper-cased and cut to 15 characters: a host named bp-00516 seeded "BP-00516".
    #[cfg(windows)]
    {
        let mut buffer = [0_u16; 256];
        let mut length = buffer.len() as u32;
        // SAFETY: length holds the buffer's size in characters; on success it is the name's.
        let format = win::COMPUTER_NAME_PHYSICAL_DNS_HOSTNAME;
        if unsafe { win::GetComputerNameExW(format, buffer.as_mut_ptr(), &mut length) } == 0 {
            return None;
        }
        Some(String::from_utf16_lossy(&buffer[..length as usize])).filter(|name| !name.is_empty())
    }
}

/// CPU seconds this process has used, user plus system.
pub(crate) fn cpu_seconds() -> f64 {
    // SAFETY: both calls write only into the structs they are given.
    unsafe {
        #[cfg(unix)]
        {
            let mut usage: libc::rusage = std::mem::zeroed();
            if libc::getrusage(libc::RUSAGE_SELF, &mut usage) != 0 {
                return 0.0;
            }
            let seconds = |value: libc::timeval| value.tv_sec as f64 + value.tv_usec as f64 / 1_000_000.0;
            seconds(usage.ru_utime) + seconds(usage.ru_stime)
        }
        #[cfg(windows)]
        {
            let mut times: [win::FileTime; 4] = std::mem::zeroed();
            let [created, exited, kernel, user] = &mut times;
            if win::GetProcessTimes(win::GetCurrentProcess(), created, exited, kernel, user) == 0 {
                return 0.0;
            }
            // FILETIME counts 100 ns ticks.
            let ticks = |time: &win::FileTime| (u64::from(time.high) << 32 | u64::from(time.low)) as f64;
            (ticks(kernel) + ticks(user)) / 10_000_000.0
        }
    }
}

/// The one-minute load average, where the system keeps one (Windows does not).
pub(crate) fn load_average() -> Option<f64> {
    #[cfg(unix)]
    {
        let mut loads = [0.0_f64; 3];
        // SAFETY: getloadavg writes at most the 3 elements it is asked for.
        let got = unsafe { libc::getloadavg(loads.as_mut_ptr(), 3) };
        (got >= 1).then_some(loads[0])
    }
    #[cfg(windows)]
    {
        None
    }
}

/// Stop a child where it is (`SIGSTOP`), so a transcode that is far enough ahead uses no CPU.
pub(crate) fn pause(pid: u32) {
    #[cfg(unix)]
    // SAFETY: kill only sends a signal.
    unsafe {
        libc::kill(pid as i32, libc::SIGSTOP);
    }
    #[cfg(windows)]
    win::with_process(pid, win::PROCESS_SUSPEND_RESUME, |handle| unsafe {
        win::NtSuspendProcess(handle);
    });
}

/// Let a paused child run again (`SIGCONT`). Harmless on one that is running.
pub(crate) fn resume(pid: u32) {
    #[cfg(unix)]
    // SAFETY: kill only sends a signal.
    unsafe {
        libc::kill(pid as i32, libc::SIGCONT);
    }
    #[cfg(windows)]
    win::with_process(pid, win::PROCESS_SUSPEND_RESUME, |handle| unsafe {
        win::NtResumeProcess(handle);
    });
}

/// Kill a process that is not our `Child` (a leftover from an earlier run).
pub(crate) fn kill(pid: u32) {
    resume(pid);
    #[cfg(unix)]
    // SAFETY: kill only sends a signal.
    unsafe {
        libc::kill(pid as i32, libc::SIGKILL);
    }
    #[cfg(windows)]
    win::with_process(pid, win::PROCESS_TERMINATE, |handle| unsafe {
        win::TerminateProcess(handle, 1);
    });
}

/// `secrets.token_bytes`: the system's random source (a machineIdentifier, the manager's token).
/// `bytes` as the whole of `path`, or `path` as it was: written beside it, flushed to disk and
/// renamed over it, so a crash or a full disk never leaves half a file. `private` makes it
/// owner-only from its first byte on Unix (a Windows profile's folders are their user's).
pub(crate) fn write_atomic(path: &std::path::Path, bytes: &[u8], private: bool) -> io::Result<()> {
    use std::io::Write;
    // Through a symlink to the file it names, and with the mode the file had: a catalog kept
    // 0600 or linked from elsewhere stays so.
    let resolved = std::fs::canonicalize(path).ok();
    let path = resolved.as_deref().unwrap_or(path);
    let kept = (!private).then(|| std::fs::metadata(path).ok().map(|meta| meta.permissions())).flatten();
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".writing");
    let temporary = path.with_file_name(name);
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    if private {
        use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
        options.mode(0o600);
        // A file left from an earlier attempt keeps its mode through `open`.
        if temporary.exists() {
            std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600))?;
        }
    }
    #[cfg(not(unix))]
    let _ = private;
    let mut file = options.open(&temporary)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    drop(file);
    if let Some(permissions) = kept {
        std::fs::set_permissions(&temporary, permissions)?;
    }
    // A failed rename leaves no half-made file behind to refuse the next write.
    std::fs::rename(&temporary, path).inspect_err(|_| {
        let _ = std::fs::remove_file(&temporary);
    })
}

pub(crate) fn random_bytes(buffer: &mut [u8]) -> io::Result<()> {
    #[cfg(unix)]
    {
        use std::io::Read;
        std::fs::File::open("/dev/urandom")?.read_exact(buffer)
    }
    #[cfg(windows)]
    {
        // SAFETY: the buffer is valid for its whole length; 2 is BCRYPT_USE_SYSTEM_PREFERRED_RNG.
        let status = unsafe {
            win::BCryptGenRandom(std::ptr::null_mut(), buffer.as_mut_ptr(), buffer.len() as u32, 2)
        };
        if status == 0 {
            Ok(())
        } else {
            Err(io::Error::other(format!("BCryptGenRandom failed: {status:#x}")))
        }
    }
}

/// Copy the DER certificates from Windows' current-user ROOT store, which includes inherited
/// machine roots. The returned bytes own their storage because `CertEnumCertificatesInStore`
/// invalidates the previous context as it advances.
#[cfg(windows)]
pub(crate) fn system_root_certificates() -> io::Result<Vec<Vec<u8>>> {
    let store_name = "ROOT\0".encode_utf16().collect::<Vec<_>>();
    // SAFETY: the store name is NUL-terminated for the duration of the call. Each context is
    // copied before asking Windows for the next one, and the store is closed on every path.
    unsafe {
        let store = win::CertOpenSystemStoreW(std::ptr::null_mut(), store_name.as_ptr());
        if store.is_null() {
            return Err(io::Error::last_os_error());
        }
        let mut certificates = Vec::new();
        let mut previous = std::ptr::null();
        let mut failure = None;
        loop {
            let context = win::CertEnumCertificatesInStore(store, previous);
            if context.is_null() {
                break;
            }
            let certificate = &*context;
            if certificate.pbCertEncoded.is_null() || certificate.cbCertEncoded == 0 {
                // CertCloseStore does not free contexts returned by enumeration. The normal
                // next enumeration frees `previous`; this error path has no next call.
                win::CertFreeCertificateContext(context);
                failure = Some(io::Error::new(io::ErrorKind::InvalidData, "Windows ROOT certificate has no DER data"));
                break;
            }
            if certificate_allows_server_auth(context) {
                certificates.push(
                    std::slice::from_raw_parts(certificate.pbCertEncoded, certificate.cbCertEncoded as usize)
                        .to_vec(),
                );
            }
            previous = context;
        }
        let close_error = (win::CertCloseStore(store, 0) == 0).then(io::Error::last_os_error);
        if let Some(error) = failure {
            return Err(error);
        }
        if let Some(error) = close_error {
            return Err(error);
        }
        Ok(certificates)
    }
}

#[cfg(windows)]
fn certificate_allows_server_auth(context: *const win::CertContext) -> bool {
    use std::ffi::CStr;

    const CRYPT_E_NOT_FOUND: u32 = 0x8009_2004;
    const SERVER_AUTH: &[u8] = b"1.3.6.1.5.5.7.3.1";

    // CertVerifyTimeValidity returns zero when the certificate is currently valid. A nonzero
    // result means it is not yet valid or has expired.
    if context.is_null() || unsafe { (*context).pCertInfo.is_null() } {
        return false;
    }
    if unsafe { win::CertVerifyTimeValidity(std::ptr::null(), (*context).pCertInfo) } != 0 {
        return false;
    }

    let mut size = 0_u32;
    // SAFETY: the first call only asks crypt32 for the buffer size.
    let status = unsafe { win::CertGetEnhancedKeyUsage(context, 0, std::ptr::null_mut(), &mut size) };
    if status == 0 || (size as usize) < std::mem::size_of::<win::CertEnhancedKeyUsage>() {
        return false;
    }

    // The returned struct and its OID pointers are packed into the caller-provided buffer.
    // Vec<usize> gives the buffer the pointer alignment required by CERT_ENHKEY_USAGE and
    // leaves the storage alive while the OID strings are inspected.
    let words = (size as usize).div_ceil(std::mem::size_of::<usize>());
    let mut buffer = vec![0_usize; words];
    let mut fill_size = size;
    // SAFETY: buffer is aligned and at least the size crypt32 requested; crypt32 fills the
    // struct and points its OID strings into that same live buffer.
    let fill_status = unsafe {
        win::CertGetEnhancedKeyUsage(
            context,
            0,
            buffer.as_mut_ptr().cast(),
            &mut fill_size,
        )
    };
    let fill_error = unsafe { win::GetLastError() };
    if fill_status == 0 {
        return false;
    }
    let usage = unsafe { &*buffer.as_ptr().cast::<win::CertEnhancedKeyUsage>() };
    if usage.cUsageIdentifier == 0 {
        return fill_error == CRYPT_E_NOT_FOUND;
    }
    let identifiers = if usage.rgpszUsageIdentifier.is_null() {
        &[][..]
    } else {
        // SAFETY: crypt32 supplied exactly cUsageIdentifier NUL-terminated OID pointers in
        // the buffer described by the successful call above.
        unsafe {
            std::slice::from_raw_parts(usage.rgpszUsageIdentifier, usage.cUsageIdentifier as usize)
        }
    };
    identifiers.iter().any(|identifier| {
        if identifier.is_null() {
            return false;
        }
        // SAFETY: enhanced-key-usage identifiers are NUL-terminated ANSI OIDs from crypt32.
        let identifier = unsafe { CStr::from_ptr(*identifier) }.to_bytes();
        identifier == SERVER_AUTH
    })
}

// The manager's process control. `rex manager` is Linux and macOS only, as manager.py was:
// it supervises servers as process groups of their own and stops them with signals.

/// `subprocess.Popen(start_new_session=True)`: the child leads a session and process
/// group of its own, so a signal to the manager's terminal or group never reaches it.
#[cfg(unix)]
pub(crate) fn new_session(command: &mut std::process::Command) {
    use std::os::unix::process::CommandExt;
    // SAFETY: setsid is async-signal-safe, all a pre_exec hook may call.
    unsafe {
        command.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
}

/// `os.killpg(pid, SIGTERM)`, or SIGKILL with `kill`; a group that is gone is skipped.
#[cfg(unix)]
pub(crate) fn signal_group(pid: u32, kill: bool) {
    // SAFETY: killpg only sends a signal.
    unsafe {
        libc::killpg(pid as libc::pid_t, if kill { libc::SIGKILL } else { libc::SIGTERM });
    }
}

/// `os.kill(pid, 0)`: whether the process exists (one we may not signal still does).
#[cfg(unix)]
pub(crate) fn alive(pid: u32) -> bool {
    // SAFETY: signal 0 only checks the process.
    let delivered = unsafe { libc::kill(pid as libc::pid_t, 0) } == 0;
    delivered || io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
}

/// `signal.Signals(number).name`.
#[cfg(unix)]
pub(crate) fn signal_name(number: i32) -> Option<&'static str> {
    [
        (libc::SIGHUP, "SIGHUP"),
        (libc::SIGINT, "SIGINT"),
        (libc::SIGQUIT, "SIGQUIT"),
        (libc::SIGILL, "SIGILL"),
        (libc::SIGTRAP, "SIGTRAP"),
        (libc::SIGABRT, "SIGABRT"),
        (libc::SIGBUS, "SIGBUS"),
        (libc::SIGFPE, "SIGFPE"),
        (libc::SIGKILL, "SIGKILL"),
        (libc::SIGUSR1, "SIGUSR1"),
        (libc::SIGSEGV, "SIGSEGV"),
        (libc::SIGUSR2, "SIGUSR2"),
        (libc::SIGPIPE, "SIGPIPE"),
        (libc::SIGALRM, "SIGALRM"),
        (libc::SIGTERM, "SIGTERM"),
        (libc::SIGXCPU, "SIGXCPU"),
    ]
    .into_iter()
    .find_map(|(signal, name)| (signal == number).then_some(name))
}

/// `ps -o command= -p PID`: a process's command line, empty when it is gone.
#[cfg(unix)]
pub(crate) fn command_line(pid: u32) -> String {
    std::process::Command::new("ps")
        .args(["-o", "command=", "-p", &pid.to_string()])
        .output()
        .map(|output| String::from_utf8_lossy(&output.stdout).into_owned())
        .unwrap_or_default()
}

/// Set once SIGTERM or SIGINT arrives, after the first call installs the handlers.
#[cfg(unix)]
pub(crate) fn termination_flag() -> &'static std::sync::atomic::AtomicBool {
    static TERMINATED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    extern "C" fn on_signal(_: libc::c_int) {
        TERMINATED.store(true, std::sync::atomic::Ordering::SeqCst);
    }
    let handler = on_signal as extern "C" fn(libc::c_int) as libc::sighandler_t;
    // SAFETY: the handler only stores to an atomic, which is async-signal-safe.
    unsafe {
        libc::signal(libc::SIGTERM, handler);
        libc::signal(libc::SIGINT, handler);
    }
    &TERMINATED
}

/// `Discovery._open`'s socket: `SO_REUSEADDR`, and `SO_REUSEPORT` where it exists, set
/// BEFORE the bind -- which is what lets it share the port with a real PMS on the same
/// host, as every Plex process there does.
#[cfg(unix)]
pub(crate) fn gdm_socket(port: u16) -> io::Result<UdpSocket> {
    use std::os::fd::FromRawFd;
    // SAFETY: plain socket-API calls on a descriptor this function owns until it hands
    // it to `UdpSocket`, which then closes it.
    unsafe {
        let fd = libc::socket(libc::AF_INET, libc::SOCK_DGRAM, 0);
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        let one: libc::c_int = 1;
        let size = std::mem::size_of::<libc::c_int>() as libc::socklen_t;
        let option = &one as *const libc::c_int as *const libc::c_void;
        libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_REUSEADDR, option, size);
        libc::setsockopt(fd, libc::SOL_SOCKET, libc::SO_REUSEPORT, option, size);
        let mut address: libc::sockaddr_in = std::mem::zeroed();
        address.sin_family = libc::AF_INET as libc::sa_family_t;
        address.sin_port = port.to_be();
        address.sin_addr = libc::in_addr { s_addr: libc::INADDR_ANY };
        if libc::bind(
            fd,
            &address as *const libc::sockaddr_in as *const libc::sockaddr,
            std::mem::size_of::<libc::sockaddr_in>() as libc::socklen_t,
        ) < 0
        {
            let error = io::Error::last_os_error();
            libc::close(fd);
            return Err(error);
        }
        Ok(UdpSocket::from_raw_fd(fd))
    }
}

/// On Windows `SO_REUSEADDR` lets ANY process take the port over, so the socket binds
/// plainly: GDM then cannot share its port with a Plex Media Server on the same machine,
/// and logs the bind error as it does on Unix.
#[cfg(windows)]
pub(crate) fn gdm_socket(port: u16) -> io::Result<UdpSocket> {
    UdpSocket::bind(("0.0.0.0", port))
}

#[cfg(windows)]
#[allow(non_snake_case)]
mod win {
    pub(super) type Handle = *mut core::ffi::c_void;
    pub(super) type CertStore = *mut core::ffi::c_void;

    #[repr(C)]
    pub(super) struct FileTime {
        pub(super) low: u32,
        pub(super) high: u32,
    }

    #[repr(C)]
    pub(super) struct CertContext {
        pub(super) dwCertEncodingType: u32,
        pub(super) pbCertEncoded: *const u8,
        pub(super) cbCertEncoded: u32,
        pub(super) pCertInfo: *const core::ffi::c_void,
        pub(super) hCertStore: CertStore,
    }

    #[repr(C)]
    pub(super) struct CertEnhancedKeyUsage {
        pub(super) cUsageIdentifier: u32,
        pub(super) rgpszUsageIdentifier: *mut *mut i8,
    }

    pub(super) const PROCESS_TERMINATE: u32 = 0x0001;
    pub(super) const PROCESS_SUSPEND_RESUME: u32 = 0x0800;
    pub(super) const COMPUTER_NAME_PHYSICAL_DNS_HOSTNAME: u32 = 5;

    #[link(name = "kernel32")]
    extern "system" {
        pub(super) fn GetCurrentThreadId() -> u32;
        pub(super) fn GetLastError() -> u32;
        pub(super) fn SetLastError(error: u32);
        pub(super) fn GetCurrentProcess() -> Handle;
        pub(super) fn GetProcessTimes(
            process: Handle,
            creation: *mut FileTime,
            exit: *mut FileTime,
            kernel: *mut FileTime,
            user: *mut FileTime,
        ) -> i32;
        pub(super) fn OpenProcess(access: u32, inherit: i32, pid: u32) -> Handle;
        pub(super) fn TerminateProcess(process: Handle, code: u32) -> i32;
        pub(super) fn CloseHandle(handle: Handle) -> i32;
        pub(super) fn GetComputerNameExW(format: u32, buffer: *mut u16, size: *mut u32) -> i32;
    }

    // Undocumented but stable since Windows XP, and what every process manager calls:
    // Win32 has no call that suspends a whole process.
    #[link(name = "ntdll")]
    extern "system" {
        pub(super) fn NtSuspendProcess(process: Handle) -> i32;
        pub(super) fn NtResumeProcess(process: Handle) -> i32;
    }

    extern "C" {
        pub(super) fn _mktime64(tm: *mut libc::tm) -> i64;
    }

    #[link(name = "bcrypt")]
    extern "system" {
        pub(super) fn BCryptGenRandom(algorithm: Handle, buffer: *mut u8, size: u32, flags: u32) -> i32;
    }

    #[link(name = "crypt32")]
    extern "system" {
        pub(super) fn CertOpenSystemStoreW(provider: Handle, store_name: *const u16) -> CertStore;
        pub(super) fn CertEnumCertificatesInStore(store: CertStore, previous: *const CertContext) -> *const CertContext;
        pub(super) fn CertCreateCertificateContext(
            encoding: u32,
            encoded: *const u8,
            length: u32,
        ) -> *const CertContext;
        pub(super) fn CertFreeCertificateContext(context: *const CertContext) -> i32;
        pub(super) fn CertSetEnhancedKeyUsage(context: *const CertContext, usage: *const CertEnhancedKeyUsage) -> i32;
        pub(super) fn CertCloseStore(store: CertStore, flags: u32) -> i32;
        pub(super) fn CertGetEnhancedKeyUsage(
            context: *const CertContext,
            flags: u32,
            usage: *mut CertEnhancedKeyUsage,
            size: *mut u32,
        ) -> i32;
        pub(super) fn CertVerifyTimeValidity(
            time: *const FileTime,
            info: *const core::ffi::c_void,
        ) -> i32;
    }

    /// Open `pid` with `access`, run `body` on the handle, close it. A process that is
    /// gone, or not ours to open, is skipped.
    pub(super) fn with_process(pid: u32, access: u32, body: impl FnOnce(Handle)) {
        // SAFETY: the handle is checked before use and closed once.
        unsafe {
            let handle = OpenProcess(access, 0, pid);
            if !handle.is_null() {
                body(handle);
                CloseHandle(handle);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use std::process::Command;
    use std::time::Duration;

    #[test]
    fn an_atomic_write_replaces_the_whole_file_and_leaves_nothing_beside_it() {
        let directory = std::env::temp_dir().join(format!("rex-atomic-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&directory);
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("claim_state.json");
        std::fs::write(&path, "an older, longer claim").unwrap();
        super::write_atomic(&path, b"new", true).unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), b"new");
        assert_eq!(std::fs::read_dir(&directory).unwrap().count(), 1);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        }
        #[cfg(unix)]
        {
            // A catalog behind a link, kept 0640: written through the link, mode kept.
            use std::os::unix::fs::PermissionsExt;
            let real = directory.join("catalog.real.json");
            let link = directory.join("catalog.json");
            std::fs::write(&real, "old").unwrap();
            std::fs::set_permissions(&real, std::fs::Permissions::from_mode(0o640)).unwrap();
            std::os::unix::fs::symlink(&real, &link).unwrap();
            super::write_atomic(&link, b"new", false).unwrap();
            assert!(std::fs::symlink_metadata(&link).unwrap().file_type().is_symlink());
            assert_eq!(std::fs::read(&real).unwrap(), b"new");
            assert_eq!(std::fs::metadata(&real).unwrap().permissions().mode() & 0o777, 0o640);
        }
        let _ = std::fs::remove_dir_all(&directory);
    }

    /// A child that appends a byte to `path` every 20 ms, so its file shows whether it runs.
    fn writer(path: &std::path::Path) -> std::process::Child {
        let python = if cfg!(windows) { "python" } else { "python3" };
        let script = format!(
            "import time\nwhile True:\n    open({:?}, 'a').write('x')\n    time.sleep(0.02)\n",
            path.to_string_lossy()
        );
        Command::new(python).arg("-c").arg(script).spawn().expect("python to run the writer")
    }

    fn size(path: &std::path::Path) -> u64 {
        std::fs::metadata(path).map_or(0, |meta| meta.len())
    }

    #[test]
    fn a_paused_child_stops_and_a_resumed_one_runs_again() {
        let path = std::env::temp_dir().join(format!("rex-pause-{}.txt", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let mut child = writer(&path);
        // Python can take seconds to start on a cold CI runner (it did on windows-2022).
        let grew_past = |floor: u64| {
            let deadline = std::time::Instant::now() + Duration::from_secs(20);
            while size(&path) <= floor && std::time::Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(50));
            }
            size(&path) > floor
        };
        assert!(grew_past(0), "the writer never wrote");
        super::pause(child.id());
        std::thread::sleep(Duration::from_millis(300)); // a write in flight may still land
        let paused = size(&path);
        std::thread::sleep(Duration::from_millis(600));
        assert_eq!(size(&path), paused, "a paused child kept writing");
        super::resume(child.id());
        assert!(grew_past(paused), "a resumed child did not run again");
        super::kill(child.id());
        let status = child.wait().expect("the killed child to be reaped");
        assert!(!status.success());
        let _ = std::fs::remove_file(&path);
    }

    #[cfg(windows)]
    #[test]
    fn cert_context_matches_windows_sdk_layout() {
        use std::mem::{align_of, offset_of, size_of};

        let pointer = size_of::<*const u8>();
        let alignment = align_of::<*const u8>();
        let align = |offset: usize| (offset + alignment - 1) & !(alignment - 1);
        let encoded = align(size_of::<u32>());
        let length = encoded + pointer;
        let info = align(length + size_of::<u32>());
        let store = info + pointer;
        assert_eq!(offset_of!(super::win::CertContext, dwCertEncodingType), 0);
        assert_eq!(offset_of!(super::win::CertContext, pbCertEncoded), encoded);
        assert_eq!(offset_of!(super::win::CertContext, cbCertEncoded), length);
        assert_eq!(offset_of!(super::win::CertContext, pCertInfo), info);
        assert_eq!(offset_of!(super::win::CertContext, hCertStore), store);
        assert_eq!(size_of::<super::win::CertContext>(), store + pointer);

        let usage_identifiers = align(size_of::<u32>());
        assert_eq!(offset_of!(super::win::CertEnhancedKeyUsage, cUsageIdentifier), 0);
        assert_eq!(offset_of!(super::win::CertEnhancedKeyUsage, rgpszUsageIdentifier), usage_identifiers);
        assert_eq!(size_of::<super::win::CertEnhancedKeyUsage>(), usage_identifiers + pointer);
    }

    #[cfg(windows)]
    #[test]
    fn windows_roots_require_server_auth_and_current_validity() {
        fn allows(der: &[u8]) -> bool {
            let context = unsafe {
                super::win::CertCreateCertificateContext(
                    1,
                    der.as_ptr(),
                    u32::try_from(der.len()).expect("fixture fits in a certificate context"),
                )
            };
            assert!(!context.is_null(), "fixture should decode as a certificate");
            let allowed = super::certificate_allows_server_auth(context);
            unsafe {
                super::win::CertFreeCertificateContext(context);
            }
            allowed
        }

        assert!(allows(include_bytes!("../../tests/fixtures/windows/server-auth.der")));
        assert!(!allows(include_bytes!("../../tests/fixtures/windows/code-signing.der")));
        assert!(allows(include_bytes!("../../tests/fixtures/windows/all-uses.der")));
        assert!(!allows(include_bytes!("../../tests/fixtures/windows/expired-server-auth.der")));
    }

    #[cfg(windows)]
    #[test]
    fn windows_eku_properties_filter_server_auth_without_store_mutation() {
        use std::ffi::CString;

        const CRYPT_E_NOT_FOUND: u32 = 0x8009_2004;
        const SERVER_AUTH: &[u8] = b"1.3.6.1.5.5.7.3.1";
        const CODE_SIGNING: &[u8] = b"1.3.6.1.5.5.7.3.3";
        let all_uses = include_bytes!("../../tests/fixtures/windows/all-uses.der");

        fn context(der: &[u8]) -> *const super::win::CertContext {
            let context = unsafe {
                super::win::CertCreateCertificateContext(
                    1,
                    der.as_ptr(),
                    u32::try_from(der.len()).expect("fixture fits in a certificate context"),
                )
            };
            assert!(!context.is_null(), "fixture should decode as a certificate");
            context
        }

        fn property_restricted(der: &[u8], oid: &[u8]) -> bool {
            let context = context(der);
            let oid = CString::new(oid).expect("OID has no NUL bytes");
            let mut identifiers = [oid.as_ptr().cast_mut()];
            let usage = super::win::CertEnhancedKeyUsage {
                cUsageIdentifier: 1,
                rgpszUsageIdentifier: identifiers.as_mut_ptr(),
            };
            let set = unsafe { super::win::CertSetEnhancedKeyUsage(context, &usage) };
            assert_ne!(set, 0, "in-memory EKU property should be set");
            let allowed = super::certificate_allows_server_auth(context);
            unsafe {
                super::win::CertFreeCertificateContext(context);
            }
            allowed
        }

        assert!(property_restricted(all_uses, SERVER_AUTH));
        assert!(!property_restricted(all_uses, CODE_SIGNING));

        let all_context = context(all_uses);
        assert!(super::certificate_allows_server_auth(all_context));
        let disabled = super::win::CertEnhancedKeyUsage {
            cUsageIdentifier: 0,
            rgpszUsageIdentifier: std::ptr::null_mut(),
        };
        let set = unsafe { super::win::CertSetEnhancedKeyUsage(all_context, &disabled) };
        assert_ne!(set, 0, "in-memory disabled EKU property should be set");
        // A zero-count property is no-valid-uses when the fill reports S_OK. Prime the thread
        // error to catch helpers that reuse a stale error instead of reading it after the fill.
        unsafe {
            super::win::SetLastError(CRYPT_E_NOT_FOUND);
        }
        assert!(!super::certificate_allows_server_auth(all_context));
        unsafe {
            super::win::CertFreeCertificateContext(all_context);
        }
    }
}
