//! What rex asks of the operating system beyond `std`: local time, measured CPU and
//! memory counters, the host name, a thread's id as Python reports it, pausing and
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

/// A single counter snapshot. CPU percentages require two successful snapshots;
/// memory is instantaneous. Missing counters never become fabricated zeroes.
#[derive(Clone, Debug)]
pub(crate) struct ResourceSample {
    at: std::time::Instant,
    host_cpu: Option<(u64, u64)>, // busy and total ticks, identical units
    process_cpu: Option<f64>, // user + kernel seconds
    cores: usize,
    host_memory: Option<f64>,
    process_memory: Option<f64>,
}

impl ResourceSample {
    pub(crate) fn utilization(&self, previous: Option<&Self>) -> [Option<f64>; 4] {
        let host = previous.and_then(|previous| {
            let (busy, total) = self.host_cpu?;
            let (old_busy, old_total) = previous.host_cpu?;
            let delta = total.checked_sub(old_total)?;
            let busy = busy.checked_sub(old_busy)?;
            (delta > 0 && busy <= delta).then(|| busy as f64 * 100.0 / delta as f64)
        });
        let process = previous.and_then(|previous| {
            let elapsed = self.at.checked_duration_since(previous.at)?.as_secs_f64();
            let delta = self.process_cpu? - previous.process_cpu?;
            (elapsed > 0.0 && delta >= 0.0 && self.cores > 0 && self.cores == previous.cores)
                .then(|| (delta / elapsed / self.cores as f64 * 100.0).min(100.0))
        });
        [host, process, self.host_memory, self.process_memory]
    }
}

fn memory_ratio(used: u64, total: u64) -> Option<f64> {
    (total > 0 && used <= total).then(|| used as f64 * 100.0 / total as f64)
}

pub(crate) fn resource_sample() -> ResourceSample {
    let mut sample = ResourceSample {
        at: std::time::Instant::now(),
        host_cpu: None,
        process_cpu: None,
        cores: 0,
        host_memory: None,
        process_memory: None,
    };
    #[cfg(unix)]
    unsafe {
        let cores = libc::sysconf(libc::_SC_NPROCESSORS_ONLN);
        if cores > 0 {
            sample.cores = cores as usize;
        }
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) == 0 {
            sample.process_cpu = Some(
                usage.ru_utime.tv_sec as f64 + usage.ru_utime.tv_usec as f64 / 1_000_000.0
                + usage.ru_stime.tv_sec as f64 + usage.ru_stime.tv_usec as f64 / 1_000_000.0,
            );
        }
    }
    #[cfg(target_os = "linux")]
    linux_resources(&mut sample);
    #[cfg(target_os = "macos")]
    mac_resources(&mut sample);
    #[cfg(windows)]
    windows_resources(&mut sample);
    sample
}

#[cfg(target_os = "linux")]
fn linux_resources(sample: &mut ResourceSample) {
    use std::io::Read;
    // /proc/stat can be large on many-core hosts; only its first (aggregate) line
    // matters. Every read is capped, including on an unexpectedly mounted procfs.
    let read = |path: &str, cap| -> Option<String> {
        let mut text = String::new();
        std::fs::File::open(path).ok()?.take(cap).read_to_string(&mut text).ok()?;
        Some(text)
    };
    sample.host_cpu = read("/proc/stat", 4096).and_then(|text| linux_cpu(&text));
    if let Some((total, available)) = read("/proc/meminfo", 16384).and_then(|text| linux_memory(&text)) {
        sample.host_memory = total.checked_sub(available).and_then(|used| memory_ratio(used, total));
        let pages = unsafe { libc::sysconf(libc::_SC_PAGESIZE) };
        if pages > 0 {
            let resident = read("/proc/self/statm", 1024)
                .and_then(|text| text.split_whitespace().nth(1)?.parse::<u64>().ok());
            sample.process_memory = resident
                .and_then(|resident| resident.checked_mul(pages as u64))
                .and_then(|bytes| memory_ratio(bytes, total));
        }
    }
}

#[cfg(any(target_os = "linux", test))]
fn linux_cpu(text: &str) -> Option<(u64, u64)> {
    let mut fields = text.lines().next()?.split_whitespace();
    if fields.next()? != "cpu" {
        return None;
    }
    // guest and guest_nice are already included in user/nice. Do not double count.
    let mut counters = [0_u64; 8];
    for (index, counter) in counters.iter_mut().enumerate() {
        *counter = match fields.next() {
            Some(value) => value.parse().ok()?,
            None if index >= 4 => 0,
            None => return None,
        };
    }
    let total = counters.iter().try_fold(0_u64, |sum, value| sum.checked_add(*value))?;
    let idle = counters[3].checked_add(counters[4])?;
    Some((total.checked_sub(idle)?, total))
}

#[cfg(any(target_os = "linux", test))]
fn linux_memory(text: &str) -> Option<(u64, u64)> {
    let field = |name| {
        let mut fields = text.lines().find(|line| line.starts_with(name))?.split_whitespace();
        fields.next()?;
        let value = fields.next()?.parse::<u64>().ok()?;
        (fields.next()? == "kB").then(|| value.checked_mul(1024)).flatten()
    };
    let total = field("MemTotal:")?;
    let available = field("MemAvailable:")?;
    (total > 0 && available <= total).then_some((total, available))
}

#[cfg(target_os = "macos")]
#[allow(deprecated)]
fn mac_resources(sample: &mut ResourceSample) {
    // libc exposes the acquisition/query APIs but not the matching port release.
    unsafe extern "C" {
        fn mach_port_deallocate(task: libc::mach_port_t, name: libc::mach_port_t) -> libc::kern_return_t;
    }
    // All Mach output buffers have their documented type and word count. The
    // send right returned by mach_host_self is released even if a query fails.
    unsafe {
        let host = libc::mach_host_self();
        let mut cpu: libc::host_cpu_load_info = std::mem::zeroed();
        let mut count = libc::HOST_CPU_LOAD_INFO_COUNT;
        if libc::host_statistics(host, libc::HOST_CPU_LOAD_INFO, (&mut cpu as *mut libc::host_cpu_load_info).cast(), &mut count) == 0 {
            let total = cpu.cpu_ticks.iter().map(|ticks| u64::from(*ticks)).sum::<u64>();
            sample.host_cpu = Some((total - u64::from(cpu.cpu_ticks[libc::CPU_STATE_IDLE as usize]), total));
        }
        let mut total = 0_u64;
        let mut size = std::mem::size_of_val(&total);
        if libc::sysctlbyname(c"hw.memsize".as_ptr(), (&mut total as *mut u64).cast(), &mut size, std::ptr::null_mut(), 0) == 0 && total > 0 {
            let mut vm: libc::vm_statistics64 = std::mem::zeroed();
            let mut count = libc::HOST_VM_INFO64_COUNT;
            let page_size = libc::sysconf(libc::_SC_PAGESIZE);
            if page_size > 0 && libc::host_statistics64(host, libc::HOST_VM_INFO64, (&mut vm as *mut libc::vm_statistics64).cast(), &mut count) == 0 {
                // Inactive/file cache is reclaimable. Compressed pages occupy RAM;
                // count their physical size, not the uncompressed stored-page count.
                let used = u64::from(vm.active_count) + u64::from(vm.wire_count) + u64::from(vm.compressor_page_count);
                sample.host_memory = used.checked_mul(page_size as u64).and_then(|used| memory_ratio(used, total));
            }
            let mut task: libc::mach_task_basic_info = std::mem::zeroed();
            let mut count = libc::MACH_TASK_BASIC_INFO_COUNT;
            if libc::task_info(libc::mach_task_self(), libc::MACH_TASK_BASIC_INFO, (&mut task as *mut libc::mach_task_basic_info).cast(), &mut count) == 0 {
                sample.process_memory = memory_ratio(task.resident_size, total);
            }
        }
        mach_port_deallocate(libc::mach_task_self(), host);
    }
}

#[cfg(windows)]
fn windows_resources(sample: &mut ResourceSample) {
    unsafe {
        let mut times: [win::FileTime; 4] = std::mem::zeroed();
        let [created, exited, kernel, user] = &mut times;
        let ticks = |time: &win::FileTime| u64::from(time.high) << 32 | u64::from(time.low);
        if win::GetProcessTimes(win::GetCurrentProcess(), created, exited, kernel, user) != 0 {
            sample.process_cpu = Some((ticks(kernel) as f64 + ticks(user) as f64) / 10_000_000.0);
        }
        let mut idle: win::FileTime = std::mem::zeroed();
        if win::GetSystemTimes(&mut idle, kernel, user) != 0 {
            // GetSystemTimes kernel includes idle.
            sample.host_cpu = ticks(kernel).checked_add(ticks(user))
                .and_then(|total| total.checked_sub(ticks(&idle)).map(|busy| (busy, total)));
        }
        let cores = win::GetActiveProcessorCount(0xffff);
        if cores > 0 {
            sample.cores = cores as usize;
            // On >64 CPUs GetSystemTimes describes only the calling group.
            if cores > 64 {
                sample.host_cpu = None;
            }
        }
        let mut memory: win::MemoryStatus = std::mem::zeroed();
        memory.length = std::mem::size_of_val(&memory) as u32;
        if win::GlobalMemoryStatusEx(&mut memory) != 0 {
            sample.host_memory = memory.total_physical.checked_sub(memory.available_physical)
                .and_then(|used| memory_ratio(used, memory.total_physical));
            let mut process: win::ProcessMemory = std::mem::zeroed();
            process.cb = std::mem::size_of_val(&process) as u32;
            if win::K32GetProcessMemoryInfo(win::GetCurrentProcess(), &mut process, process.cb) != 0 {
                sample.process_memory = memory_ratio(process.working_set as u64, memory.total_physical);
            }
        }
    }
}

/// Hand memory the allocator holds free back to the system. glibc keeps what a large free
/// left behind: a production rex that let go of 1.3 GB of restored rows kept 910 MB resident
/// for 340 MB in use (2026-10-24). Elsewhere the allocator returns it on its own.
pub(crate) fn release_free_memory() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    // SAFETY: malloc_trim only returns free pages; it touches no live allocation.
    unsafe {
        libc::malloc_trim(0);
    }
}

/// Two malloc arenas, not glibc's eight a core: those suit programs allocating on many threads
/// at once, and rex does most of its work behind the runtime lock. The production server sat at
/// 730 MB resident in 42 arena heaps for a 286 MB heap (2026-11-01). An operator's
/// `MALLOC_ARENA_MAX` wins, as glibc reads it itself. Called before any thread starts.
pub fn limit_malloc_arenas() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    if std::env::var_os("MALLOC_ARENA_MAX").is_none() {
        // SAFETY: mallopt only sets a tunable.
        unsafe {
            libc::mallopt(libc::M_ARENA_MAX, 2);
        }
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
    pub(super) struct MemoryStatus {
        pub(super) length: u32,
        pub(super) load: u32,
        pub(super) total_physical: u64,
        pub(super) available_physical: u64,
        pub(super) total_page_file: u64,
        pub(super) available_page_file: u64,
        pub(super) total_virtual: u64,
        pub(super) available_virtual: u64,
        pub(super) available_extended_virtual: u64,
    }

    #[repr(C)]
    pub(super) struct ProcessMemory {
        pub(super) cb: u32,
        pub(super) page_fault_count: u32,
        pub(super) peak_working_set: usize,
        pub(super) working_set: usize,
        pub(super) quota_peak_paged_pool: usize,
        pub(super) quota_paged_pool: usize,
        pub(super) quota_peak_nonpaged_pool: usize,
        pub(super) quota_nonpaged_pool: usize,
        pub(super) page_file: usize,
        pub(super) peak_page_file: usize,
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
        pub(super) fn GetSystemTimes(idle: *mut FileTime, kernel: *mut FileTime, user: *mut FileTime) -> i32;
        pub(super) fn GetActiveProcessorCount(group: u16) -> u32;
        pub(super) fn GlobalMemoryStatusEx(status: *mut MemoryStatus) -> i32;
        pub(super) fn K32GetProcessMemoryInfo(process: Handle, counters: *mut ProcessMemory, size: u32) -> i32;
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

    fn fixture() -> serde_json::Value {
        serde_json::from_str(include_str!("../../tests/fixtures/dashboard_os_counters.json")).unwrap()
    }

    #[test]
    fn captured_linux_counters_exclude_guest_and_use_available_memory() {
        let fixture = fixture();
        let linux = &fixture["linux"];
        let before = super::linux_cpu(linux["cpu_before"].as_str().unwrap()).unwrap();
        let after = super::linux_cpu(linux["cpu_after"].as_str().unwrap()).unwrap();
        assert_eq!(after.0 - before.0, 3944);
        assert_eq!(after.1 - before.1, 3944); // all observed ticks busy
        let (total, available) = super::linux_memory(linux["meminfo"].as_str().unwrap()).unwrap();
        assert_eq!(total, 7_976_692 * 1024);
        assert_eq!(available, 3_687_908 * 1024);
        let ratio = super::memory_ratio(total - available, total).unwrap();
        assert!(ratio > 53.0 && ratio < 54.0);
        let rss = linux["statm"].as_str().unwrap().split_whitespace().nth(1).unwrap().parse::<u64>().unwrap()
            * linux["page_bytes"].as_u64().unwrap();
        assert!(super::memory_ratio(rss, total).unwrap() > 0.0);
        // These guest counters must not alter either total or busy.
        assert_eq!(super::linux_cpu("cpu 1 2 3 4 5 6 7 8 100 200"), Some((27, 36)));
    }

    #[test]
    fn captured_macos_memory_counts_physical_compressor_pages() {
        let fixture = fixture();
        let mac = &fixture["macos"];
        let used = ["active_pages", "wired_pages", "compressor_pages"].into_iter()
            .map(|key| mac[key].as_u64().unwrap()).sum::<u64>() * mac["page_bytes"].as_u64().unwrap();
        let ratio = super::memory_ratio(used, mac["total_bytes"].as_u64().unwrap()).unwrap();
        assert!(ratio > 65.0 && ratio < 66.0);
    }

    #[test]
    fn resource_delta_normalizes_cpu_and_rejects_unavailable_or_reset_counters() {
        let at = std::time::Instant::now();
        let before = super::ResourceSample {
            at, host_cpu: Some((100, 200)), process_cpu: Some(1.0),
            cores: 4, host_memory: Some(60.0), process_memory: Some(2.0),
        };
        let mut after = super::ResourceSample {
            at: at + Duration::from_secs(2), host_cpu: Some((150, 400)),
            process_cpu: Some(3.0), ..before.clone()
        };
        assert_eq!(after.utilization(Some(&before)), [Some(25.0), Some(25.0), Some(60.0), Some(2.0)]);
        assert_eq!(before.utilization(None), [None, None, Some(60.0), Some(2.0)]);
        assert_eq!(&before.utilization(Some(&before))[..2], &[None, None]);
        after.host_cpu = Some((1, 2));
        after.process_cpu = Some(0.0);
        assert_eq!(&after.utilization(Some(&before))[..2], &[None, None]);
        after.host_cpu = None;
        after.process_cpu = None;
        assert_eq!(&after.utilization(Some(&before))[..2], &[None, None]);
    }

    #[test]
    fn malformed_os_counters_are_not_idle_or_zero_memory() {
        for text in ["", "cpu 1 2", "cpu 1 bad 3 4", "cpu 18446744073709551615 1 1 1", "cpu0 1 2 3 4"] {
            assert_eq!(super::linux_cpu(text), None);
        }
        for text in ["", "MemTotal: 0 kB\nMemAvailable: 0 kB", "MemTotal: 1 kB\nMemAvailable: 2 kB", "MemTotal: bad kB", "MemTotal: 2 B\nMemAvailable: 1 B"] {
            assert_eq!(super::linux_memory(text), None);
        }
        assert_eq!(super::memory_ratio(1, 0), None);
        assert_eq!(super::memory_ratio(11, 10), None);
        assert_eq!(super::memory_ratio(0, 10), Some(0.0));
    }

    /// Run only on an isolated supported OS host, not production. Exercises this
    /// test process's real CPU time and touched resident pages, without children.
    #[test]
    #[ignore = "real-process metrics smoke; run explicitly on an isolated host"]
    fn real_process_resource_smoke() {
        let before = super::resource_sample();
        let mut memory = vec![0_u8; 16 * 1024 * 1024];
        let deadline = std::time::Instant::now() + Duration::from_millis(750);
        let mut value = 1_u64;
        while std::time::Instant::now() < deadline {
            for byte in memory.iter_mut().step_by(4096) {
                value = value.wrapping_mul(6364136223846793005).wrapping_add(1);
                *byte = std::hint::black_box(value as u8);
            }
        }
        std::hint::black_box(&memory);
        let after = super::resource_sample();
        let metrics = after.utilization(Some(&before));
        for metric in metrics {
            let metric = metric.expect("supported OS must expose its counters");
            assert!(metric.is_finite() && (0.0..=100.0).contains(&metric));
        }
        assert!(metrics[1].unwrap() > 0.0);
        assert!(metrics[2].unwrap() > 0.0);
        assert!(metrics[3].unwrap() > 0.0);
    }

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
