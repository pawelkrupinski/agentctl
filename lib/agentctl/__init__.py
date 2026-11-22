"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`wire_store(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("usage", "core", "timefmt", "statefile", "moviestools", "classify", "selfcode", "ghapp", "forge", "repos",
           "buildview", "notifyurl", "config", "workflows", "trailers", "waitingdir", "redretired", "storetoken",
           "claudefiles", "gating", "allowedpaths", "procs", "fixerrecords", "inboxentry", "wait", "cpu",
           "fixersession", "live", "owned", "clones", "worktrees", "fixerenv", "deletions", "machine", "govtickets",
           "testtmp", "procwatch", "inbox", "jvmreap", "maint", "notifier", "patches", "redworkflows", "redrepos",
           "redfamilies", "cistate", "govbudget", "ancestry", "ciwhy", "launchd", "beats", "sdnotify", "storeauth",
           "tickrun", "gatesummary", "dispatchblock", "statemachines", "store", "policy", "redrecord", "lander",
           "leases", "land", "stats", "explorer", "queueslots", "redfixread", "reverts", "dispatches", "cleanup",
           "conditions", "upkeep", "queue", "generated", "conflicts", "fixchange", "queuerecheck", "redlink",
           "redfixes", "redreplay", "issues", "identitychange", "changekind", "owners", "redask", "laptoprole",
           "fixerland", "fixer", "queueuntil", "red", "redmove", "waiting", "queuesubmit", "landwatch",
           "rundispatch", "blameopen", "remotestore", "tunnel", "storestate", "sessionpaths", "serverclone",
           "landhold", "recordingpins", "redauthors", "culpritfix", "remotejobs", "remote", "redprobe",
           "processorlink", "tickservice", "runexplain", "runsigs", "branches", "outsideflows", "redseen",
           "redobserve", "reduntracked", "redblock", "redopen", "redearly", "redclaim", "redverify", "redcrash",
           "fixerlife", "owner", "redvault", "blamerecords", "blame", "reddispatch", "livepage", "livestream",
           "statusagents", "pressure", "govusage", "gov", "checkslot", "batchrun", "cliops", "localrecheck",
           "queuebatch", "queuediff", "queueclose", "superseded", "park", "govroute", "govcmd", "alertroute",
           "alertprobe", "alerts", "projector", "ask", "redpending", "redstep", "redview", "cireport", "watchtick",
           "recovery", "netwatch", "tools", "batchitem", "redhold", "queuepush", "orphans", "maintain", "invariants",
           "watchdog", "batchpush", "server", "snapshot", "batch", "batchcheck", "landqueue", "status", "statuspage",
           "redstatus", "redtick", "syncsteps", "serversteps", "queueenter", "slots", "cliparser", "processor",
           "cli")
