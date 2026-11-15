//! The terminal ticket (§6.4): the only way a console socket gets a session.
//!
//! `Core.ws_ticket(id, ttl_ms)` mints one; the handshake verifies it. The token is
//! `base64url(nonce) || base64url(hmac_sha256(key, session_id ‖ 0x00 ‖ expiry_unix ‖ 0x00 ‖ nonce))`,
//! 65 URL-safe characters, and the mint remembers the nonce with its session and expiry until that
//! expiry — which is where the expiry in the MAC input comes from, and what makes a ticket single use.
//!
//! The key is 32 bytes from `BCryptGenRandom` per `Core` and never leaves the core, which is why the
//! core mints the ticket instead of Python. Verification compares the MAC in constant time
//! (`hmac`'s `verify_slice`) and answers a refusal that the handshake turns into an HTTP 4xx; no
//! socket exists for a refused token.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::sync::Mutex;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use hmac::{Hmac, Mac};
use sha2::Sha256;

use super::entropy;

type HmacSha256 = Hmac<Sha256>;

const KEY_BYTES: usize = 32;
const NONCE_BYTES: usize = 16;
/// `URL_SAFE_NO_PAD` of 16 bytes: a fixed width is what makes the token self-delimiting.
const NONCE_TEXT_CHARS: usize = 22;
const MAC_BYTES: usize = 32;
/// `URL_SAFE_NO_PAD` of 32 bytes.
const MAC_TEXT_CHARS: usize = 43;
const TOKEN_CHARS: usize = NONCE_TEXT_CHARS + MAC_TEXT_CHARS;

/// The hard bound on remembered nonces. It is a cap on memory, not on the hub: a console that reloads
/// more often than this within one TTL is not a console, and an evicted (unexpired) ticket is simply
/// refused as unknown, which the browser answers by fetching a fresh one. 4096 nonces ≈ 300 KB of
/// nonce, session id and expiry.
const MAX_TRACKED: usize = 4096;
/// `ttl_ms` comes from the caller, so it is capped: no ticket outlives this, and the table's pruning
/// therefore always drains. The hub asks for 30 s (§6.4).
const MAX_TTL: Duration = Duration::from_secs(300);

/// What `ws_ticket` hands the hub: the token and the unix second it stops working in.
#[derive(Clone, Debug)]
pub struct Minted {
    pub token: String,
    pub expires_at_unix: u64,
}

/// Why a token was refused. The handshake maps each onto an HTTP status; the console then takes its
/// existing reconnect path.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// No nonce this core minted, or a token that is not one of ours at all.
    Unknown,
    /// The mint's expiry has passed (`ttl_ms` is short: a stale console tab must not reconnect into it).
    Expired,
    /// The token was minted for another session id than the path names.
    WrongSession,
    /// The MAC does not cover this id, expiry and nonce with this core's key.
    BadMac,
    /// A second use of one ticket.
    Reused,
}

impl Refusal {
    /// The status the handshake answers with (§6.4: an HTTP 4xx, no socket). `403` is the one case
    /// where the caller proved it holds a ticket and asked for a different session.
    pub fn status(self) -> u16 {
        match self {
            Refusal::WrongSession => 403,
            Refusal::Unknown | Refusal::Expired | Refusal::BadMac | Refusal::Reused => 401,
        }
    }
}

impl std::fmt::Display for Refusal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let text = match self {
            Refusal::Unknown => "unknown ticket",
            Refusal::Expired => "ticket expired",
            Refusal::WrongSession => "ticket belongs to another session",
            Refusal::BadMac => "ticket signature does not verify",
            Refusal::Reused => "ticket already used",
        };
        f.write_str(text)
    }
}

struct Issued {
    session_id: String,
    expires_at_unix: u64,
    used: bool,
}

/// The nonces the store remembers, with their expiries ordered so pruning and the cap are a pop, not
/// a scan: `by_expiry` carries one `(expiry, nonce)` per live entry, and a stale pair left by an
/// evicted nonce is discarded when it surfaces.
#[derive(Default)]
struct Table {
    entries: HashMap<String, Issued>,
    by_expiry: BinaryHeap<Reverse<(u64, String)>>,
}

impl Table {
    /// Forgets every entry whose expiry has passed. Amortized: each entry is popped once.
    fn prune(&mut self, now: u64) {
        while let Some(Reverse((expiry, nonce))) = self.by_expiry.peek() {
            if *expiry >= now {
                break;
            }
            let expiry = *expiry;
            let nonce = nonce.clone();
            self.by_expiry.pop();
            if self.entries.get(&nonce).map(|entry| entry.expires_at_unix) == Some(expiry) {
                self.entries.remove(&nonce);
            }
        }
    }

    /// Makes room for one more entry: the soonest-expiring nonce goes first, so the cap costs the
    /// ticket closest to its own expiry. `prune` has already run, so the table is at `MAX_TRACKED`
    /// live entries at most.
    fn make_room(&mut self) {
        while self.entries.len() >= MAX_TRACKED {
            let Some(Reverse((expiry, nonce))) = self.by_expiry.pop() else {
                return;
            };
            if self.entries.get(&nonce).map(|entry| entry.expires_at_unix) == Some(expiry) {
                self.entries.remove(&nonce);
            }
        }
    }
}

/// The per-core ticket key and the nonces it has minted (§6.4: "remembers the nonce until expiry so a
/// ticket is used once").
pub struct Tickets {
    key: [u8; KEY_BYTES],
    issued: Mutex<Table>,
}

impl Tickets {
    /// One key per `Core`, from the OS CSPRNG. `Err` means the core cannot authenticate anything and
    /// must not start (`Core::new` propagates it).
    pub fn new() -> Result<Self, String> {
        let mut key = [0u8; KEY_BYTES];
        entropy::fill_random(&mut key)?;
        Ok(Self {
            key,
            issued: Mutex::new(Table::default()),
        })
    }

    /// Mints one ticket for `session_id`, valid for `ttl` (capped at [`MAX_TTL`]). The expiry is
    /// rounded up to a whole unix second, so a `ttl` below 1 s still produces a usable token.
    pub fn mint(&self, session_id: &str, ttl: Duration) -> Result<Minted, String> {
        let ttl = ttl.min(MAX_TTL);
        let now = now_unix();
        let expires_at_unix = now + ttl.as_secs() + u64::from(ttl.subsec_nanos() > 0);
        let mut nonce = [0u8; NONCE_BYTES];
        entropy::fill_random(&mut nonce)?;
        let mac = self.mac(session_id, expires_at_unix, &nonce).finalize().into_bytes();
        let nonce_text = URL_SAFE_NO_PAD.encode(nonce);
        let token = format!("{nonce_text}{}", URL_SAFE_NO_PAD.encode(mac));
        let mut issued = self.issued.lock().unwrap_or_else(|error| error.into_inner());
        // The table lives exactly as long as the tickets in it: an expired nonce can be forgotten, a
        // used one must stay until its expiry so a replay cannot be mistaken for a fresh mint. Both
        // bounds are a heap pop, never a walk of the table.
        issued.prune(now);
        issued.make_room();
        issued
            .by_expiry
            .push(Reverse((expires_at_unix, nonce_text.clone())));
        issued.entries.insert(
            nonce_text,
            Issued {
                session_id: session_id.to_string(),
                expires_at_unix,
                used: false,
            },
        );
        Ok(Minted {
            token,
            expires_at_unix,
        })
    }

    /// Verifies a handshake token for the path's `session_id` and consumes it: `Ok` at most once per
    /// mint. Every check the design names is here — the MAC (constant time), the expiry, the ticket's
    /// id against the path's, and the single use.
    pub fn consume(&self, session_id: &str, token: &str) -> Result<(), Refusal> {
        if token.len() != TOKEN_CHARS {
            return Err(Refusal::Unknown);
        }
        let (nonce_text, mac_text) = token.split_at(NONCE_TEXT_CHARS);
        let nonce = URL_SAFE_NO_PAD.decode(nonce_text).map_err(|_| Refusal::Unknown)?;
        let given = URL_SAFE_NO_PAD.decode(mac_text).map_err(|_| Refusal::Unknown)?;
        if nonce.len() != NONCE_BYTES || given.len() != MAC_BYTES {
            return Err(Refusal::Unknown);
        }
        let mut issued = self.issued.lock().unwrap_or_else(|error| error.into_inner());
        // The asked token keeps its own answer (`Expired`, not `Unknown`) while the rest of the table
        // gives back what has expired, so a store that outlived its tickets drains even when minting
        // has stopped.
        let now = now_unix();
        if issued
            .entries
            .get(nonce_text)
            .is_some_and(|entry| now > entry.expires_at_unix)
        {
            issued.entries.remove(nonce_text);
            return Err(Refusal::Expired);
        }
        issued.prune(now);
        let entry = issued.entries.get_mut(nonce_text).ok_or(Refusal::Unknown)?;
        if entry.session_id != session_id {
            return Err(Refusal::WrongSession);
        }
        if now_unix() > entry.expires_at_unix {
            return Err(Refusal::Expired);
        }
        let expected = self.mac(session_id, entry.expires_at_unix, &nonce);
        // Constant time: `verify_slice` compares through `subtle` and fails on a length mismatch.
        expected.verify_slice(&given).map_err(|_| Refusal::BadMac)?;
        if entry.used {
            return Err(Refusal::Reused);
        }
        entry.used = true;
        Ok(())
    }

    fn mac(&self, session_id: &str, expires_at_unix: u64, nonce: &[u8]) -> HmacSha256 {
        let mut mac = HmacSha256::new_from_slice(&self.key)
            .expect("HMAC-SHA256 accepts a key of any length");
        mac.update(session_id.as_bytes());
        mac.update(&[0u8]);
        mac.update(expires_at_unix.to_string().as_bytes());
        mac.update(&[0u8]);
        mac.update(nonce);
        mac
    }

    #[cfg(test)]
    /// How many nonces the table still holds: the bounds the mint and the consume keep.
    pub fn tracked(&self) -> usize {
        self.issued
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .entries
            .len()
    }
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|elapsed| elapsed.as_secs())
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tickets() -> Tickets {
        Tickets::new().expect("key")
    }

    #[test]
    fn a_minted_ticket_verifies_once_and_is_then_reused() {
        let tickets = tickets();
        let minted = tickets.mint("session-a", Duration::from_secs(30)).expect("mint");
        assert_eq!(minted.token.len(), TOKEN_CHARS);
        assert!(minted.token.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_'));
        assert_eq!(tickets.consume("session-a", &minted.token), Ok(()));
        assert_eq!(tickets.consume("session-a", &minted.token), Err(Refusal::Reused));
    }

    #[test]
    fn every_token_is_different() {
        let tickets = tickets();
        let first = tickets.mint("session-a", Duration::from_secs(30)).expect("mint");
        let second = tickets.mint("session-a", Duration::from_secs(30)).expect("mint");
        assert_ne!(first.token, second.token, "the nonce did not change");
    }

    #[test]
    fn an_expired_ticket_is_refused() {
        let tickets = tickets();
        let minted = tickets.mint("session-a", Duration::ZERO).expect("mint");
        // `mint` rounds up to the next second, so wait for the one it was minted in to end.
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while now_unix() <= minted.expires_at_unix {
            assert!(std::time::Instant::now() < deadline, "the clock never passed the expiry");
            std::thread::sleep(Duration::from_millis(20));
        }
        assert_eq!(tickets.consume("session-a", &minted.token), Err(Refusal::Expired));
    }

    #[test]
    fn a_ticket_of_one_session_is_refused_for_another() {
        let tickets = tickets();
        let minted = tickets.mint("session-a", Duration::from_secs(30)).expect("mint");
        assert_eq!(
            tickets.consume("session-b", &minted.token),
            Err(Refusal::WrongSession)
        );
        // The refusal must not have consumed it: the right id still works.
        assert_eq!(tickets.consume("session-a", &minted.token), Ok(()));
    }

    #[test]
    fn a_flipped_mac_byte_is_refused() {
        let tickets = tickets();
        let minted = tickets.mint("session-a", Duration::from_secs(30)).expect("mint");
        // Flip one bit of one MAC byte, re-encoded so the tampering survives base64's padding bits.
        let (nonce_text, mac_text) = minted.token.split_at(NONCE_TEXT_CHARS);
        let mut mac = URL_SAFE_NO_PAD.decode(mac_text).expect("mac");
        mac[0] ^= 0x80;
        let tampered = format!("{nonce_text}{}", URL_SAFE_NO_PAD.encode(&mac));
        assert_eq!(tickets.consume("session-a", &tampered), Err(Refusal::BadMac));
        // A tampered token must stay unconsumed so an honest retry still works.
        assert_eq!(tickets.consume("session-a", &minted.token), Ok(()));
    }

    #[test]
    fn a_token_this_core_never_minted_is_unknown_and_malformed_ones_too() {
        let tickets = tickets();
        assert_eq!(tickets.consume("session-a", ""), Err(Refusal::Unknown));
        assert_eq!(tickets.consume("session-a", "short"), Err(Refusal::Unknown));
        // A well-formed but foreign token: another core's nonce is not in this table.
        let other = Tickets::new().expect("key");
        let foreign = other.mint("session-a", Duration::from_secs(30)).expect("mint");
        assert_eq!(tickets.consume("session-a", &foreign.token), Err(Refusal::Unknown));
        // Padding characters are not ours either.
        let padded = format!("{}=", foreign.token);
        assert_eq!(tickets.consume("session-a", &padded), Err(Refusal::Unknown));
    }

    #[test]
    fn expired_nonces_are_pruned_by_the_next_mint() {
        let tickets = tickets();
        let minted = tickets.mint("session-a", Duration::ZERO).expect("mint");
        assert_eq!(tickets.tracked(), 1);
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while now_unix() <= minted.expires_at_unix {
            assert!(std::time::Instant::now() < deadline, "the clock never passed the expiry");
            std::thread::sleep(Duration::from_millis(20));
        }
        let _fresh = tickets.mint("session-b", Duration::from_secs(30)).expect("mint");
        assert_eq!(tickets.tracked(), 1, "the expired nonce was kept");
    }

    /// `consume` prunes as well, so a store whose minting stopped still gives expired nonces back.
    #[test]
    fn expired_nonces_are_pruned_by_a_consume_too() {
        let store = tickets();
        let minted = store.mint("session-a", Duration::ZERO).expect("mint");
        let deadline = std::time::Instant::now() + Duration::from_secs(3);
        while now_unix() <= minted.expires_at_unix {
            assert!(std::time::Instant::now() < deadline, "the clock never passed the expiry");
            std::thread::sleep(Duration::from_millis(20));
        }
        // A well-formed token this core never minted reaches the table, and the prune runs first.
        let elsewhere = tickets();
        let elsewhere = elsewhere.mint("session-a", Duration::from_secs(30)).expect("mint");
        assert_eq!(store.consume("session-a", &elsewhere.token), Err(Refusal::Unknown));
        assert_eq!(store.tracked(), 0, "the expired nonce outlived a consume");
    }

    /// The store is a bounded table: past the cap the soonest-expiring nonce is evicted, and the
    /// survivor is still one use.
    #[test]
    fn the_ticket_table_is_capped_and_evicts_the_soonest_expiry() {
        let tickets = tickets();
        let doomed = tickets.mint("session-a", Duration::from_secs(2)).expect("mint");
        assert_eq!(tickets.tracked(), 1);
        let mut last = doomed.clone();
        for _ in 0..(MAX_TRACKED + 16) {
            last = tickets.mint("session-a", MAX_TTL).expect("mint");
        }
        assert_eq!(tickets.tracked(), MAX_TRACKED, "the store grew past its cap");
        assert_eq!(
            tickets.consume("session-a", &doomed.token),
            Err(Refusal::Unknown),
            "the soonest-expiring nonce was not the one evicted"
        );
        assert_eq!(tickets.consume("session-a", &last.token), Ok(()));
        assert_eq!(tickets.consume("session-a", &last.token), Err(Refusal::Reused));
    }

    /// `ttl_ms` is caller-controlled, so an unbounded one would pin a nonce for as long as it liked.
    #[test]
    fn a_tickets_lifetime_is_capped() {
        let tickets = tickets();
        let minted = tickets.mint("session-a", Duration::from_secs(86_400)).expect("mint");
        let longest = now_unix() + MAX_TTL.as_secs() + 1;
        assert!(
            minted.expires_at_unix <= longest,
            "a day-long ttl outlived the cap: {} > {longest}",
            minted.expires_at_unix
        );
    }
}
