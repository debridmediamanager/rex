//! What rex asks of the operating system beyond `std`: local time, the process's CPU time
//! and the host's load, the host name, a thread's id as Python reports it, pausing and
//! killing a child, and the GDM socket.
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
    #[cfg(windows)]
    {
        std::env::var("COMPUTERNAME").ok().filter(|name| !name.is_empty())
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

    #[repr(C)]
    pub(super) struct FileTime {
        pub(super) low: u32,
        pub(super) high: u32,
    }

    pub(super) const PROCESS_TERMINATE: u32 = 0x0001;
    pub(super) const PROCESS_SUSPEND_RESUME: u32 = 0x0800;

    #[link(name = "kernel32")]
    extern "system" {
        pub(super) fn GetCurrentThreadId() -> u32;
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
        std::thread::sleep(Duration::from_millis(800));
        assert!(size(&path) > 0, "the writer never wrote");
        super::pause(child.id());
        std::thread::sleep(Duration::from_millis(300)); // a write in flight may still land
        let paused = size(&path);
        std::thread::sleep(Duration::from_millis(600));
        assert_eq!(size(&path), paused, "a paused child kept writing");
        super::resume(child.id());
        std::thread::sleep(Duration::from_millis(600));
        assert!(size(&path) > paused, "a resumed child did not run again");
        super::kill(child.id());
        let status = child.wait().expect("the killed child to be reaped");
        assert!(!status.success());
        let _ = std::fs::remove_file(&path);
    }
}
