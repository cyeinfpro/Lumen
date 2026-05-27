use anyhow::Result;

pub struct SleepGuard {
    inner: platform::PlatformSleepGuard,
}

impl SleepGuard {
    pub fn new() -> Self {
        Self {
            inner: platform::PlatformSleepGuard::new(),
        }
    }

    pub fn set_active(&mut self, active: bool) -> Result<()> {
        self.inner.set_active(active)
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use anyhow::{anyhow, Result};
    use std::ffi::CString;
    use std::os::raw::{c_char, c_void};
    use std::ptr;

    const CFSTRING_ENCODING_UTF8: u32 = 0x0800_0100;
    const IOPM_ASSERTION_LEVEL_ON: u32 = 255;
    const IOPM_ASSERTION_TYPE_PREVENT_IDLE_SYSTEM_SLEEP: &str = "PreventUserIdleSystemSleep";
    const IOPM_ASSERTION_NAME: &str = "Lumen.app";

    type CFStringRef = *const c_void;
    type CFTypeRef = *const c_void;

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringCreateWithCString(
            alloc: *const c_void,
            c_str: *const c_char,
            encoding: u32,
        ) -> CFStringRef;
        fn CFRelease(cf: CFTypeRef);
    }

    #[link(name = "IOKit", kind = "framework")]
    extern "C" {
        fn IOPMAssertionCreateWithName(
            assertion_type: CFStringRef,
            assertion_level: u32,
            assertion_name: CFStringRef,
            assertion_id: *mut u32,
        ) -> i32;
        fn IOPMAssertionRelease(assertion_id: u32) -> i32;
    }

    struct CfString(CFStringRef);

    impl CfString {
        fn new(value: &str) -> Result<Self> {
            let c_string = CString::new(value)?;
            let cf_string = unsafe {
                CFStringCreateWithCString(ptr::null(), c_string.as_ptr(), CFSTRING_ENCODING_UTF8)
            };
            if cf_string.is_null() {
                return Err(anyhow!("create CFString for sleep assertion"));
            }
            Ok(Self(cf_string))
        }

        fn as_ref(&self) -> CFStringRef {
            self.0
        }
    }

    impl Drop for CfString {
        fn drop(&mut self) {
            if !self.0.is_null() {
                unsafe {
                    CFRelease(self.0 as CFTypeRef);
                }
            }
        }
    }

    pub struct PlatformSleepGuard {
        assertion_id: Option<u32>,
    }

    impl PlatformSleepGuard {
        pub fn new() -> Self {
            Self { assertion_id: None }
        }

        pub fn set_active(&mut self, active: bool) -> Result<()> {
            match (active, self.assertion_id) {
                (true, None) => self.acquire(),
                (false, Some(_)) => self.release(),
                _ => Ok(()),
            }
        }

        fn acquire(&mut self) -> Result<()> {
            let assertion_type = CfString::new(IOPM_ASSERTION_TYPE_PREVENT_IDLE_SYSTEM_SLEEP)?;
            let assertion_name = CfString::new(IOPM_ASSERTION_NAME)?;
            let mut assertion_id = 0_u32;
            let result = unsafe {
                IOPMAssertionCreateWithName(
                    assertion_type.as_ref(),
                    IOPM_ASSERTION_LEVEL_ON,
                    assertion_name.as_ref(),
                    &mut assertion_id,
                )
            };
            if result != 0 {
                return Err(anyhow!(
                    "IOPMAssertionCreateWithName failed with status {result}"
                ));
            }
            self.assertion_id = Some(assertion_id);
            Ok(())
        }

        fn release(&mut self) -> Result<()> {
            let Some(assertion_id) = self.assertion_id.take() else {
                return Ok(());
            };
            let result = unsafe { IOPMAssertionRelease(assertion_id) };
            if result != 0 {
                self.assertion_id = Some(assertion_id);
                return Err(anyhow!("IOPMAssertionRelease failed with status {result}"));
            }
            Ok(())
        }
    }

    impl Drop for PlatformSleepGuard {
        fn drop(&mut self) {
            let _ = self.release();
        }
    }
}

#[cfg(windows)]
mod platform {
    use anyhow::{anyhow, Result};

    type ExecutionState = u32;

    const ES_CONTINUOUS: ExecutionState = 0x8000_0000;
    const ES_SYSTEM_REQUIRED: ExecutionState = 0x0000_0001;

    #[link(name = "kernel32")]
    extern "system" {
        fn SetThreadExecutionState(flags: ExecutionState) -> ExecutionState;
    }

    pub struct PlatformSleepGuard {
        active: bool,
    }

    impl PlatformSleepGuard {
        pub fn new() -> Self {
            Self { active: false }
        }

        pub fn set_active(&mut self, active: bool) -> Result<()> {
            if self.active == active {
                return Ok(());
            }
            let flags = if active {
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED
            } else {
                ES_CONTINUOUS
            };
            let previous = unsafe { SetThreadExecutionState(flags) };
            if previous == 0 {
                return Err(anyhow!("SetThreadExecutionState failed"));
            }
            self.active = active;
            Ok(())
        }
    }

    impl Drop for PlatformSleepGuard {
        fn drop(&mut self) {
            if self.active {
                let _ = unsafe { SetThreadExecutionState(ES_CONTINUOUS) };
            }
        }
    }
}

#[cfg(not(any(target_os = "macos", windows)))]
mod platform {
    use anyhow::Result;

    pub struct PlatformSleepGuard;

    impl PlatformSleepGuard {
        pub fn new() -> Self {
            Self
        }

        pub fn set_active(&mut self, _active: bool) -> Result<()> {
            Ok(())
        }
    }
}
