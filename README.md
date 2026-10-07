# Server Sentinel — Free Edition

Your modded Minecraft server crashed. Did it crash the *same way* as last time?

```bash
python sentinel_free.py watch logs/latest.log
```

Read-only. It changes nothing, starts nothing and stops nothing — it follows the
log and tells you what is happening. One file, no dependencies, Python 3.8+.

Or install it from PyPI and skip the download:

```bash
pip install server-sentinel
server-sentinel watch logs/latest.log
```

## Why this exists

Every hosting panel restarts a dead server. None of them notice it is dying the
same way every time.

The usual shape: a mod crashes on a block or entity that is still in the world.
The server restarts, loads that chunk, and crashes identically. Every 40 seconds,
all night, until the disk fills with identical crash reports or your host
suspends you for the restart churn.

This tells you that is happening:

```
[02:14:07] CRASH (repeat #2)
           cause:   java.lang.NullPointerException: Cannot invoke "Entity.getX()"
           culprit: examplemod-2.1.11.jar
           sig:     0429a917c82d

           THIS IS THE SAME CRASH AS 1 TIME(S) ALREADY.
           Restarting will reproduce it. Fix the cause above first.
```

## How the signature works

Comparing crash reports by text never matches — the same bug reports a different
line number, index, coordinate or entity id every single time it fires. So the
signature is built from:

- the **root cause** (the last `Caused by:`, or the top exception) with every run
  of digits replaced, so `Index 42` and `Index 7` collapse to the same thing;
- plus the **first non-vanilla jar** in the stack trace — Forge stamps every
  frame with the jar it came from, so the first one that is not Minecraft,
  Forge, the JDK or a bundled library is almost always the mod at fault.

Both directions matter. Too loose and it calls unrelated crashes identical; too
tight and it never notices a loop. Verified: the same bug at a different line
signs identically, while a different exception or the same exception in a
different mod signs differently.

**When it cannot build one, it says so.** A crash with no readable cause *and*
no identifiable jar gets no signature, and the report says
`sig: none - this crash cannot be fingerprinted`. Repeats of that crash will not
be detected, and you are told that rather than left to assume otherwise — the
alternative is announcing two unrelated crashes as the same one, which is the
failure that would actually cost you something: it stops you restarting a server
that would have come straight back up.

## What it also reports

- Server up, with boot time, and server stopping
- Players joining and leaving, with a running count
- Lag spikes — but only real ones. `Can't keep up!` at 2000ms during chunk
  generation is normal and is deliberately ignored; a tool that cries wolf is a
  tool you learn to scroll past.
- Out of memory and watchdog hangs

## What the full version adds

This edition watches. The paid one acts:

```bash
sentinel guard "./start.sh" --max-repeats 2
```

`guard` supervises the server, restarts it when it dies, and **refuses to
restart into a crash signature it has already seen** — so one bad mod stops
being an all-night loop. Plus exponential backoff, a minimum-uptime check that
catches failed starts, webhook alerts and a JSON event log.

<https://kaiven.gumroad.com/l/server-sentinel>

## How this was built

Built by Jakoby Tuckta with Claude, against a live 234-mod Forge server. The
code was written with Claude; the server, the logs and the calls about what
shipped are mine. Every commit is tagged `Co-Authored-By: Claude`.

Worth knowing about this file in particular: twenty-one tests of the signature
function passed while `watch` was signing the **wrong lines** — the crash marker
arrives before the exception it introduces, so it was signing text that did not
contain the cause, and on a second crash it signed the *previous* crash's trace.
Two identical crashes got two different signatures, which defeats the whole
feature. Testing the function proved nothing; only driving the actual command
found it. The tests that do that ship in the paid edition.

[The full account of what the real data corrected](https://jaakoby.github.io/how-this-was-built.html).

## Honest limits

It reads logs. A native JVM crash leaves no Java trace to parse — look for an
`hs_err_pid` file instead. If your server dies with no crash report at all, the
process was killed from outside, usually the OS out-of-memory killer.

## Related reading

- [Minecraft server keeps crashing and restarting in a loop](https://jaakoby.github.io/guides/minecraft-server-keeps-restarting.html)
  — why a panel's auto-restart hides the problem instead of fixing it
- [Server won't start and there is no crash report](https://jaakoby.github.io/guides/minecraft-server-wont-start-no-crash-report.html)
  — the case the limits above describe
- [How much RAM a modded server actually needs](https://jaakoby.github.io/guides/how-much-ram-modded-minecraft-server.html)

Free to use on any server you own or administer — see `LICENSE.txt`. Not
affiliated with Mojang, Microsoft, MinecraftForge or NeoForged.
