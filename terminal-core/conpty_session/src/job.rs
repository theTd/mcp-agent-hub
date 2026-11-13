//! One job object per session, with kill-on-close, assigned before the child is resumed.
//!
//! The hub itself runs inside the desktop app's kill-on-close job (`desktop/src-tauri/src/owned_hub.rs`,
//! `HubJob::new_kill_on_close`), so these jobs are nested. Windows 8+ supports nested jobs, but an outer
//! job may refuse the assignment (`AssignProcessToJobObject` fails); the caller then keeps
//! `assigned = false` and `kill` falls back to the ToolHelp tree walk in `job_tree.rs`, which is what
//! `taskkill /T /F` did for the hub before.

use std::io;

use windows::core::BOOL;
use windows::core::PCWSTR;
use windows::Win32::Foundation::HANDLE;
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, IsProcessInJob, JobObjectExtendedLimitInformation,
    SetInformationJobObject, TerminateJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

use crate::handles::OwnedHandle;

#[derive(Debug)]
pub struct JobObject {
    handle: OwnedHandle,
}

impl JobObject {
    /// CreateJobObjectW + JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the child tree dies with the last handle.
    pub fn new_kill_on_close() -> io::Result<Self> {
        // SAFETY: anonymous job with default security; the returned handle is owned by the wrapper.
        let handle = unsafe { CreateJobObjectW(None, PCWSTR::null()) }
            .map_err(|error| io::Error::other(error.message()))?;
        let job = Self {
            handle: OwnedHandle::new(handle),
        };
        // SAFETY: an all-zero JOBOBJECT_EXTENDED_LIMIT_INFORMATION is a valid value; one flag is set
        // and the pointer/size describe that same value.
        let info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe {
            let mut info: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
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

    /// TerminateJobObject: every process in the job dies, including grandchildren.
    pub fn terminate(&self, exit_code: u32) -> io::Result<()> {
        // SAFETY: the job handle is valid and owned by this value.
        unsafe { TerminateJobObject(self.handle.get(), exit_code) }
            .map_err(|error| io::Error::other(error.message()))
    }
}
