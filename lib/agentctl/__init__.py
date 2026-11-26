"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`CurrentStore(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("procrunner", "escalationtable", "psparse", "govpool", "usage", "statefile", "selfcode", "statemachines",
           "storecalls", "clock", "ids", "timefmt", "livepage", "holdgraph", "configfile", "knobread", "watchknobs",
           "tickknobs", "storeknobs", "storeauth", "redknobs", "blameknobs", "queueknobs", "govknobs", "fixerknobs",
           "leasetable", "offers", "cpu", "core", "classify", "causes", "eventrows", "ghapp", "ghtally", "forge",
           "repos", "buildview", "notifyurl", "config", "workflows", "trailers", "redretired", "claudefiles",
           "gating", "allowedpaths", "procs", "fixerrecords", "actors", "filelog", "waitingdir", "storetoken",
           "inboxentry", "escalations", "wait", "fixersession", "live", "clones", "fixerenv", "deletions", "machine",
           "testtmp", "inbox", "jvmreap", "notifier", "patches", "landstate", "worktrees", "redworkflows",
           "redrepos", "redfamilies", "cistate", "ancestry", "ciwhy", "launchd", "beats", "sdnotify", "gatesummary",
           "policy", "redrecord", "lander", "leases", "redfixread", "maincontain", "redfixcheck", "conditions",
           "generated", "redlink", "owners", "redask", "laptoprole", "red", "landwatch", "blameopen", "sessionpaths",
           "serverclone", "recordingpins", "runexplain", "runsigs", "outsideflows", "stuckruns", "redepisode",
           "livestream", "statusagents", "alertroute", "alertprobe", "alerts", "ownerleases", "queuehold",
           "runverdict", "owned", "govtickets", "procwatch", "maint", "govbudget", "backfillexact", "selfci",
           "pressure", "govusage", "govfits", "queuestall", "cidispatches", "rundispatch", "redauthors", "cleanup",
           "upkeep", "queueuntil", "cleanuppass", "fixerblockers", "branches", "hostsleep", "eventtrace",
           "statsrecords", "statsepisodes", "stats", "remotejobs", "timeline", "checkrows", "backfill", "store",
           "land", "explorer", "reverts", "generatedregen", "conflicts", "redreplay", "issues", "fixerland",
           "remotestore", "tunnel", "storestate", "culpritfix", "sessionroll", "queuefailure", "queueslots", "queue",
           "fixchange", "queuerecheck", "redfixes", "redland", "identitychange", "fixer", "redmove", "landhold",
           "changekind", "waiting", "queuesubmit", "processorlink", "tickservice", "timelinecmd", "projector",
           "statusline", "redfollow", "gov", "checkslot", "govroute", "govcmd", "remote", "redprobe", "batchrun",
           "escalationsession", "redseen", "redobserve", "reduntracked", "redblock", "redopen", "redearly",
           "redclaim", "redverify", "blamerecords", "orphanreap", "redvault", "redownerpush", "redquota", "blame",
           "redwaits", "cliops", "localrecheck", "queuebatch", "queuediff", "queuecheck", "queueclose", "superseded",
           "park", "redcrash", "reddispatch", "reddecision", "fixerlife", "owner", "ask", "ownerwrapper",
           "offerdispatch", "redview", "cireport", "watchtick", "recovery", "netwatch", "tools", "batchitem",
           "redlease", "redpending", "redstep", "redhold", "server", "queuebatchsel", "batch", "queuepush",
           "orphans", "orphanpass", "batchpush", "batchcheck", "batchtick", "status", "maintain", "invariants",
           "watchdog", "landqueue", "statuspage", "redstatus", "syncsteps", "redcommands", "queueenter",
           "landdryrun", "slots", "cliparser", "redtick", "serversteps", "processor", "cli")
