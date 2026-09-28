//! Native plexsim components.
//!
//! The HTTP surface is intentionally not part of this first bootstrap.  The
//! decision module is the first native component because it has a complete,
//! checked-in measured corpus and no transport dependencies.

pub mod catalog;
pub mod decision;
pub mod filters;
pub mod gate;
pub mod hierarchy;
pub mod mediainfo;
pub mod metadata;
pub mod xmlout;
