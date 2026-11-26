"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`CurrentStore(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("procrunner", "govpool", "usage", "statefile", "moviestools", "selfcode", "statemachines", "clock",
           "timefmt", "storeauth", "offers", "livepage", "statsrecords", "holdgraph", "configfile", "core",
           "classify", "ghapp", "ghtally", "forge", "repos", "buildview", "notifyurl", "config", "workflows",
           "trailers", "waitingdir", "redretired", "storetoken", "claudefiles", "gating", "allowedpaths", "procs",
           "fixerrecords", "inboxentry", "wait", "cpu", "fixersession", "live", "clones", "worktrees", "fixerenv",
           "deletions", "machine", "testtmp", "inbox", "jvmreap", "notifier", "patches", "redworkflows", "redrepos",
           "redfamilies", "cistate", "ancestry", "ciwhy", "launchd", "beats", "sdnotify", "tickrun", "gatesummary",
           "dispatchblock", "store", "policy", "redrecord", "lander", "leases", "land", "explorer", "redfixread",
           "redfixcheck", "reverts", "dispatches", "conditions", "generated", "generatedregen", "conflicts",
           "redlink", "redreplay", "issues", "owners", "redask", "laptoprole", "fixerland", "red", "landwatch",
           "rundispatch", "blameopen", "remotestore", "tunnel", "storestate", "sessionpaths", "serverclone",
           "recordingpins", "redauthors", "culpritfix", "runexplain", "runsigs", "outsideflows", "stuckruns",
           "redepisode", "livestream", "statusagents", "alertroute", "alertprobe", "alerts", "ownerleases",
           "sessionroll", "redworkflow", "statsepisodes", "stats", "remotejobs", "queuefailure", "selfci",
           "queuehold", "runverdict", "owned", "govtickets", "procwatch", "maint", "govbudget", "queueslots",
           "cleanup", "upkeep", "queue", "fixchange", "queuerecheck", "redfixes", "identitychange", "fixer",
           "queueuntil", "redmove", "landhold", "branches", "changekind", "waiting", "queuesubmit", "processorlink",
           "tickservice", "redseen", "redobserve", "reduntracked", "redblock", "redopen", "redearly", "redclaim",
           "redverify", "redcrash", "blamerecords", "pressure", "govusage", "gov", "checkslot", "cliops",
           "localrecheck", "govroute", "govcmd", "projector", "orphanreap", "fixerlife", "owner", "redvault",
           "redownerpush", "ask", "reddispatch", "ownerwrapper", "cleanuppass", "statusline", "redquota", "remote",
           "redprobe", "blame", "batchrun", "queuebatch", "offerdispatch", "queuediff", "queuecheck", "redwaits",
           "redview", "cireport", "watchtick", "recovery", "queuebatchsel", "netwatch", "tools", "batchitem",
           "redlease", "batch", "redfollow", "redpending", "redstep", "queueclose", "superseded", "park", "snapshot",
           "queuepush", "orphans", "orphanpass", "batchpush", "batchcheck", "batchtick", "status", "maintain",
           "redhold", "server", "invariants", "watchdog", "landqueue", "statuspage", "redstatus", "syncsteps",
           "redcommands", "queueenter", "landdryrun", "slots", "cliparser", "redtick", "serversteps", "processor",
           "cli")
