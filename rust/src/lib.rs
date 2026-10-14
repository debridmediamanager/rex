//! Native rex components and the Rust HTTP runtime.
//!
//! The modules share the measured catalog and sidecars with the Python
//! implementation; the runtime itself never starts Python or replays a
//! response fixture.

pub mod catalog;
pub mod catalog_tool;
pub mod decision;
pub mod filters;
pub mod gate;
pub mod hierarchy;
pub mod identify;
pub mod mediainfo;
pub mod metadata;
pub(crate) mod platform;
pub mod runtime;
pub mod store;
pub mod xmlout;
