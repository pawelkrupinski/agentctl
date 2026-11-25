"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`CurrentStore(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("procrunner", "govpool", "usage", "timefmt", "statefile", "moviestools", "selfcode", "storeauth",
           "statemachines", "clock", "offers", "livepage", "statsrecords", "holdgraph", "configfile", "core",
           "classify", "ghapp", "ghtally", "forge", "repos", "buildview", "notifyurl", "config", "workflows",
           "trailers", "waitingdir", "redretired", "storetoken", "claudefiles", "gating", "allowedpaths", "procs",
           "fixerrecords", "inboxentry", "wait", "cpu", "fixersession", "live", "owned", "clones", "worktrees",
           "fixerenv", "deletions", "machine", "govtickets", "testtmp", "procwatch", "inbox", "jvmreap", "maint",
           "notifier", "patches", "redworkflows", "redrepos", "redfamilies", "cistate", "govbudget", "ancestry",
           "ciwhy", "launchd", "beats", "sdnotify", "tickrun", "gatesummary", "dispatchblock", "store", "policy",
           "redrecord", "lander", "leases", "land", "explorer", "queueslots", "redfixread", "reverts", "dispatches",
           "cleanup", "conditions", "upkeep", "queue", "generated", "generatedregen", "conflicts", "fixchange",
           "queuerecheck", "redlink", "redfixes", "redreplay", "issues", "identitychange", "owners", "redask",
           "laptoprole", "fixerland", "fixer", "queueuntil", "red", "redmove", "landwatch", "rundispatch",
           "blameopen", "remotestore", "tunnel", "storestate", "sessionpaths", "serverclone", "landhold",
           "recordingpins", "redauthors", "culpritfix", "runexplain", "runsigs", "branches", "outsideflows",
           "stuckruns", "redepisode", "changekind", "waiting", "queuesubmit", "processorlink", "tickservice",
           "redseen", "redobserve", "reduntracked", "redblock", "redopen", "redearly", "redclaim", "redverify",
           "redcrash", "blamerecords", "livestream", "statusagents", "pressure", "govusage", "gov", "checkslot",
           "cliops", "localrecheck", "govroute", "govcmd", "alertroute", "alertprobe", "alerts", "projector",
           "orphanreap", "ownerleases", "sessionroll", "fixerlife", "owner", "redvault", "redownerpush", "ask",
           "reddispatch", "ownerwrapper", "cleanuppass", "redworkflow", "statusline", "redquota", "statsepisodes",
           "stats", "remotejobs", "remote", "redprobe", "blame", "batchrun", "queuebatch", "offerdispatch",
           "queuefailure", "queuediff", "queuecheck", "queueclose", "superseded", "park", "snapshot", "selfci",
           "redwaits", "redview", "cireport", "watchtick", "recovery", "queuebatchsel", "netwatch", "tools",
           "batchitem", "redlease", "queuepush", "orphans", "orphanpass", "batchpush", "batch", "batchcheck",
           "batchtick", "status", "maintain", "redfollow", "redpending", "redstep", "redhold", "server",
           "invariants", "watchdog", "landqueue", "statuspage", "redstatus", "syncsteps", "redcommands",
           "queueenter", "landdryrun", "slots", "cliparser", "redtick", "serversteps", "processor", "cli")
