# Connecting to the Pi

`larkbridge` is a Raspberry Pi 3 Model B running Debian 13 trixie, user `admin`, with the project
checked out at `~/rpi-lark-bridge`. It reaches the bench two ways: through the lab router, or over a
direct Ethernet cable to the operator laptop. Both are supported at once and neither needs a
password — `~/.ssh/config` carries the key and three aliases.

| Alias | Target | Works when |
|---|---|---|
| `larkbridge` | `larkbridge.local` (mDNS) | **Both arrangements.** Prefer this. |
| `larkbridge-lan` | `192.168.0.251` | Pi is on the lab router |
| `larkbridge-direct` | `192.168.7.2` | Pi is on a direct cable to the laptop |

Prefer `larkbridge` unless a literal IP is required. It resolves over IPv6 link-local, which needs
no DHCP and works on the router and on a bare cable alike. It was the only address that kept
working through every failure mode observed during bring-up of the direct link, including with the
Pi stranded on the wrong subnet.

Connect non-interactively:

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 larkbridge 'hostname; ip -4 -o addr show eth0'
```

## The direct cable

`eth0` carries two NetworkManager profiles. `Wired connection 1` is DHCP at
`autoconnect-priority 100` with `dhcp-timeout=15` and `autoconnect-retries=1`.
`larkbridge-direct` is static `192.168.7.2/24` at priority 0, with `never-default=true` and no
gateway or DNS, so it can never capture the default route.

On the laptop the matching `192.168.7.1/24` comes from the Ethernet adapter's IPv4 **Alternate
Configuration** (ncpa.cpl → Ethernet → IPv4 → Alternate Configuration → User configured). That is a
Windows GUI setting recorded in no repository, and it is the only reason the laptop holds a `.7`
address. `New-NetIPAddress -PolicyStore PersistentStore` cannot replace it: Windows returns error 87
rather than persist a static address on a DHCP-enabled interface.

Moving the Pi *to* the direct cable is automatic. DHCP finds no server, fails after roughly thirty
seconds, and NetworkManager falls to the static profile.

## Traps

**Windows `ping` reports success when it has failed.** `ping 192.168.0.251` prints `0% loss` while
the reply lines read `Destination host unreachable`. Those replies come from the laptop's own
address, and Windows counts them as received packets. Never trust the summary line. Confirm with an
actual SSH command, or with `Get-NetNeighbor -IPAddress <ip>` — an unreachable host shows a
`00-00-00-00-00-00` link-layer address.

**The fallback is sticky.** Returning the Pi to the router does not restore DHCP.
`autoconnect-priority` only decides which profile NetworkManager picks when it *activates* a
connection, and a fast cable swap logs nothing but `carrier: link connected` — no teardown, so the
policy never re-runs. The Pi then sits on `192.168.7.2` on the router with no gateway and no
internet. Restore it with:

```sh
ssh larkbridge 'sudo nmcli connection up "Wired connection 1"'
```

Leaving the cable unplugged for about ten seconds before replugging usually forces the teardown and
makes the switch automatic, but that is timing-dependent and not guaranteed.

**Never configure this Pi's network with a netplan YAML.** `pi/scripts/netplan-startup-fastpath`
takes the boot fast path only when `/etc/netplan` and `/run/netplan` hold no `*.yaml`. Adding one
silently returns the ~4.4 s that [`boot-optimization-results-2026-08-19.md`](../boot-optimization-results-2026-08-19.md)
records the project buying. Use `nmcli`.

**`pi_ip` in `rig/inventory.toml` is arrangement-specific** — `192.168.0.251` on the router,
`192.168.7.2` on the direct cable — and must be flipped when the Pi moves. `pi_host` is stable;
prefer it. That file is gitignored and uses LF endings; do not convert it to CRLF.

**The direct cable has no internet and no NTP**, and the Pi 3B has no RTC, so the clock is wrong
after any reboot in that arrangement and will corrupt experiment timestamps. `NetworkManager-wait-online`
also behaves differently without a DHCP server, so boot timings taken on the direct cable are not
comparable to the router baselines. Boot-timing runs belong on the router — see
[`boot-optimization.md`](../boot-optimization.md).
