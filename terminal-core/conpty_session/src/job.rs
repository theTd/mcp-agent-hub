//! One job object per session, with kill-on-close, assigned before the child is resumed.
//!
//! The hub itself runs inside the desktop app's kill-on-close job (`desktop/src-tauri/src/owned_hub.rs`,
//! `HubJob::new_kill_on_close`), so these jobs are nested. Windows 8+ supports nested jobs, but an outer
//! job may refuse the assignment (`AssignProcessToJobObject` fails); the caller then keeps
//! `assigned = false` and `kill` falls back to the ToolHelp tree walk in `job_tree.rs`, which is what
//! `taskkill /T /F` did for the hub before.
//!
//! Which processes the job owns is a policy, not a constant. `KillTree` is the original behaviour: the
//! client and everything it starts live in the job, so terminating the job ends the whole tree. `ClientOnly`
//! adds `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`, which makes every child the client creates break away from
//! the job silently: the job then holds the client process alone, and ending it leaves the client's own
//! children alone — which is what a session whose client starts a machine-wide singleton
//! (`doubao.exe --type=service`) needs, because killing that singleton ends every concurrent session of
//! the same product.

use std::io;

use windows::core::BOOL;
use windows::core::PCWSTR;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JobObjectBasicLimitInformation,
    JobObjectExtendedLimitInformation, QueryInformationJobObject, SetInformationJobObject,
    TerminateJobObject, JOBOBJECT_BASIC_LIMIT_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK,
};

use crate::handles::OwnedHandle;

/// Which processes a session's job owns, i.e. what `TerminateJobObject` of that job ends.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum JobPolicy {
    /// The client and every descendant it starts (the original behaviour). `kill` ends the whole tree.
    #[default]
    KillTree,
    /// The client alone: `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` moves every child the client creates out
    /// of the job, so ending the session cannot reach a process the client shares with others (a
    /// machine-wide service singleton, for instance).
    ClientOnly,
}

impl JobPolicy {
    /// The `LimitFlags` this policy sets on the job: always kill-on-close (the client must not outlive the
    /// last job handle), plus silent break-away for `ClientOnly`.
    pub fn limit_flags(self) -> JOB_OBJECT_LIMIT {
        match self {
            Self::KillTree => JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            Self::ClientOnly => {
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
            }
        }
    }
}

#[derive(Debug)]
pub struct JobObject {
    handle: OwnedHandle,
    policy: JobPolicy,
}

impl JobObject {
    /// CreateJobObjectW + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the child tree dies with the last handle.
    pub fn new_kill_on_close() -> io::Result<Self> {
        Self::new_kill_on_close_with(JobPolicy::KillTree)
    }

    /// CreateJobObjectW with the limit flags of `policy`: kill-on-close always, silent break-away for
    /// [`JobPolicy::ClientOnly`].
    pub fn new_kill_on_close_with(policy: JobPolicy) -> io::Result<Self> {
        // SAFETY: anonymous job with default security; the returned handle is owned by the wrapper.
        let handle = unsafe { CreateJobObjectW(None, PCWSTR::null()) }
            .map_err(|error| io::Error::other(error.message()))?;
        let job = Self {
            handle: OwnedHandle::new(handle),
            policy,
        };
        // SAFETY: an all-zero JOBOBJECT_EXTENDED_LIMIT_INFORMATION is a valid value; the policy's flags
        // are set and the pointer/size describe that same value.
        let info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe {
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = policy.limit_flags();
            info
        };
        // SAFETY: see above; `info` is a valid pointer to the described struct.
        unsafe {
            SetInformationJobObject(
                job.handle.get(),
                JobObjectExtendedLimitInformation,
                &info as *const _ as *const core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            )
        }
        .map_err(|error| io::Error::other(error.message()))?;
        Ok(job)
    }

    pub fn handle(&self) -> HANDLE {
        self.handle.get()
    }

    /// The policy this job was created with: `kill` reads it to decide whether a tree walk is allowed.
    pub fn policy(&self) -> JobPolicy {
        self.policy
    }

    /// Puts a (suspended) process into this job. The error is the Win32 message, so the caller can log
    /// why nested assignment was refused.
    pub fn assign(&self, process: HANDLE) -> io::Result<()> {
        // SAFETY: both handles are valid for the duration of the call.
        unsafe { AssignProcessToJobObject(self.handle.get(), process) }
            .map_err(|error| io::Error::other(error.message()))
    }

    /// True when the process is currently a member of this job.
    pub fn contains(&self, process: HANDLE) -> bool {
        let mut inside = BOOL(0);
        // SAFETY: `inside` is a valid out pointer; both handles are valid for the call.
        let ok = unsafe { IsProcessInJob(process, Some(self.handle.get()), &mut inside) };
        ok.is_ok() && inside.as_bool()
    }

    /// The job's current `LimitFlags` as Windows reports them, which is what the policy test asserts on
    /// instead of trusting the value written by `new_kill_on_close_with`.
    pub fn limit_flags(&self) -> io::Result<JOB_OBJECT_LIMIT> {
        let mut info = JOBOBJECT_BASIC_LIMIT_INFORMATION::default();
        // SAFETY: `info` is a valid out pointer with the size the class expects; the job handle is owned.
        unsafe {
            QueryInformationJobObject(
                Some(self.handle.get()),
                JobObjectBasicLimitInformation,
                &mut info as *mut _ as *mut core::ffi::c_void,
                std::mem::size_of::<JOBOBJECT_BASIC_LIMIT_INFORMATION>() as u32,
                None,
            )
        }
        .map_err(|error| io::Error::other(error.message()))?;
        Ok(info.LimitFlags)
    }

    /// TerminateJobObject: every process in the job dies. Under `ClientOnly` the job holds the client
    /// alone, because its children broke away when they were created.
    pub fn terminate(&self, exit_code: u32) -> io::Result<()> {
        // SAFETY: the job handle is valid and owned by this value.
        unsafe { TerminateJobObject(self.handle.get(), exit_code) }
            .map_err(|error| io::Error::other(error.message()))
    }
}

#[cfg(test)]
mod tests {
    //! The property the `ClientOnly` fix rests on, read back from the kernel instead of from the value the
    //! constructor wrote: the job carries `JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK`, and a child of a job
    //! member therefore lands outside the job. Without it a child of the client stays in the job and
    //! `TerminateJobObject` reaches the shared service again.
    //!
    //! The two process cases below run without ConPTY on purpose: they are about the job's membership, so
    //! they can be checked on any Windows host, including one where `CreatePseudoConsole` is unavailable.

    use std::process::{Child, Command, Stdio};
    use std::time::{Duration, Instant};

    use windows::Win32::System::Threading::{
        OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SET_QUOTA, PROCESS_TERMINATE,
    };

    use super::*;
    use crate::job_tree::{children_of, process_alive, process_name};

    fn silent_breakaway_set(job: &JobObject) -> bool {
        let flags = job.limit_flags().expect("QueryInformationJobObject");
        (flags.0 & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK.0) != 0
    }

    /// Both policies keep kill-on-close: the client must never outlive the last job handle.
    fn kill_on_close_set(job: &JobObject) -> bool {
        let flags = job.limit_flags().expect("QueryInformationJobObject");
        (flags.0 & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.0) != 0
    }

    #[test]
    fn client_only_reports_silent_breakaway() {
        let job = JobObject::new_kill_on_close_with(JobPolicy::ClientOnly).expect("job");
        assert_eq!(job.policy(), JobPolicy::ClientOnly);
        assert!(
            silent_breakaway_set(&job),
            "ClientOnly must let the client's children break away: {:?}",
            job.limit_flags()
        );
        assert!(kill_on_close_set(&job), "ClientOnly keeps kill-on-close");
    }

    #[test]
    fn kill_tree_does_not_break_away() {
        let job = JobObject::new_kill_on_close().expect("job");
        assert_eq!(job.policy(), JobPolicy::KillTree, "the default is the tree policy");
        assert!(
            !silent_breakaway_set(&job),
            "KillTree must keep the client's children inside the job: {:?}",
            job.limit_flags()
        );
        assert!(kill_on_close_set(&job), "KillTree keeps kill-on-close");
    }

    #[test]
    fn the_policy_default_is_kill_tree() {
        assert_eq!(JobPolicy::default(), JobPolicy::KillTree);
    }

    /// A `cmd.exe` client that starts one long-running child of its own, the way a real client starts the
    /// shared service: `ping -n 3` is a delay (its own child, gone by itself), then the `ping -n 30` under
    /// test runs for the rest of the case. The delay is what lets the assignment below happen before the
    /// child under test is created, which is the order the real spawn path guarantees with
    /// `CREATE_SUSPENDED`.
    const CLIENT_WITH_CHILD: &str = "ping -n 3 127.0.0.1 >nul & ping -n 30 127.0.0.1 >nul";
    /// How long the case waits for a process the OS creates.
    const SPAWN_DEADLINE: Duration = Duration::from_secs(20);
    /// Past this the delay ping is gone and the only child left is the one under test.
    const DELAY_SETTLED: Duration = Duration::from_millis(3600);

    fn spawn_client() -> Child {
        Command::new("cmd.exe")
            .args(["/c", CLIENT_WITH_CHILD])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("cmd.exe")
    }

    /// A handle with the rights `AssignProcessToJobObject` needs, and one that can also query membership.
    fn open_client(pid: u32) -> Option<OwnedHandle> {
        // SAFETY: no inheritance; the handle is owned by the wrapper and closed by it.
        unsafe {
            OpenProcess(
                PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
                false,
                pid,
            )
        }
        .ok()
        .map(|handle| OwnedHandle::new(handle))
    }

    /// True while `pid` is a member of `job`, as Windows reports it.
    fn in_job(job: &JobObject, pid: u32) -> bool {
        let Some(process) = open_client(pid) else {
            return false;
        };
        job.contains(process.get())
    }

    /// The client's long-running child: the only `ping` it has left once the delay has passed.
    fn wait_for_the_clients_child(client: u32, spawned_at: Instant) -> u32 {
        let settled = spawned_at + DELAY_SETTLED;
        while Instant::now() < settled {
            std::thread::sleep(Duration::from_millis(20));
        }
        let end = Instant::now() + SPAWN_DEADLINE;
        while Instant::now() < end {
            for child in children_of(client) {
                if process_name(child).is_some_and(|name| name.eq_ignore_ascii_case("PING.EXE")) {
                    return child;
                }
            }
            std::thread::sleep(Duration::from_millis(20));
        }
        panic!("cmd.exe {client} never started its ping child");
    }

    /// Ends whatever a failing case might leave running, so no `ping` outlives the test binary.
    struct KillOnDrop(Vec<u32>);

    impl Drop for KillOnDrop {
        fn drop(&mut self) {
            for pid in &self.0 {
                crate::job_tree::terminate_tree(*pid);
            }
        }
    }

    /// A client in a job, its own child created after the assignment.
    fn client_with_a_child_in(policy: JobPolicy) -> (JobObject, Child, u32, u32) {
        let job = JobObject::new_kill_on_close_with(policy).expect("job");
        let spawned_at = Instant::now();
        let client = spawn_client();
        let client_pid = client.id();
        let handle = open_client(client_pid).expect("client handle");
        job.assign(handle.get())
            .unwrap_or_else(|error| panic!("AssignProcessToJobObject({client_pid}): {error}"));
        assert!(
            job.contains(handle.get()) && in_job(&job, client_pid),
            "the client is not in its own job, so the case proves nothing"
        );
        drop(handle);
        let child = wait_for_the_clients_child(client_pid, spawned_at);
        (job, client, client_pid, child)
    }

    /// `ClientOnly`: the client's own child is created outside the job, so terminating the job ends the
    /// client and leaves that child running — the whole point of the policy, because the real client's
    /// child is a service every other session also uses.
    #[test]
    fn client_only_leaves_the_clients_child_outside_the_job() {
        let (job, mut client, client_pid, child) = client_with_a_child_in(JobPolicy::ClientOnly);
        let _guard = KillOnDrop(vec![client_pid, child]);
        assert!(
            !in_job(&job, child),
            "ClientOnly left the client's child {child} in the job: terminating the job would kill it"
        );
        job.terminate(1).expect("TerminateJobObject");
        let end = Instant::now() + SPAWN_DEADLINE;
        while Instant::now() < end && process_alive(client_pid) {
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = client.wait();
        assert!(!process_alive(client_pid), "the client {client_pid} survived the job's termination");
        // A member would already be gone; the settle window only keeps a dying process from reading alive.
        std::thread::sleep(Duration::from_millis(300));
        assert!(
            process_alive(child),
            "the client's child {child} died with the job: it was still a member"
        );
    }

    /// `KillTree` (unchanged behaviour): the same child is a member of the job, so terminating the job
    /// ends it with the client. The two cases differ in nothing but the policy.
    #[test]
    fn kill_tree_terminates_the_clients_child() {
        let (job, mut client, client_pid, child) = client_with_a_child_in(JobPolicy::KillTree);
        let _guard = KillOnDrop(vec![client_pid, child]);
        assert!(
            in_job(&job, child),
            "KillTree must keep the client's child {child} in the job"
        );
        job.terminate(1).expect("TerminateJobObject");
        let end = Instant::now() + SPAWN_DEADLINE;
        while Instant::now() < end && (process_alive(client_pid) || process_alive(child)) {
            std::thread::sleep(Duration::from_millis(20));
        }
        let _ = client.wait();
        assert!(!process_alive(client_pid), "the client {client_pid} survived the job's termination");
        assert!(
            !process_alive(child),
            "the client's child {child} outlived the job under KillTree"
        );
    }
}
