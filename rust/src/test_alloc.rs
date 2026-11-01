//! The test binary's allocator, counting what each thread holds, so a memory test weighs what
//! a call keeps (`rows::budget_tests`, `store::tests`); the other tests allocate on threads of
//! their own. One per crate: a second `#[global_allocator]` does not compile.

use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::Cell;

thread_local! {
    static HELD: Cell<isize> = const { Cell::new(0) };
}

/// Bytes this thread allocated and has not freed.
pub(crate) fn held() -> isize {
    HELD.with(Cell::get)
}

fn count(bytes: isize) {
    let _ = HELD.try_with(|held| held.set(held.get() + bytes));
}

struct Counting;

// SAFETY: `System` does the allocating; this only counts.
unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc(layout) };
        if !ptr.is_null() {
            count(layout.size() as isize);
        }
        ptr
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if !ptr.is_null() {
            count(layout.size() as isize);
        }
        ptr
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        count(-(layout.size() as isize));
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, size: usize) -> *mut u8 {
        let moved = unsafe { System.realloc(ptr, layout, size) };
        if !moved.is_null() {
            count(size as isize - layout.size() as isize);
        }
        moved
    }
}

#[global_allocator]
static COUNTING: Counting = Counting;
