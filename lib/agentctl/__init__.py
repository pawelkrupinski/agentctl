"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`CurrentStore(process_store)`, the probes, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("procrunner", "escalationtable", "psparse", "govpool", "usage", "statefile", "selfcode", "statemachines",
           "storecalls", "clock", "filelock", "ids", "timefmt", "livepage", "holdgraph", "configfile", "knobread",
           "watchknobs", "tickknobs", "storeknobs", "storeauth", "redknobs", "blameknobs", "queueknobs", "govknobs",
           "fixerknobs", "leasetable", "offers", "cpu", "core", "ghapp", "ghtally", "repos", "buildview", "config",
           "workflows", "trailers", "claudefiles", "allowedpaths", "procs", "fixerrecords", "actors", "live",
           "clones", "machine", "jvmreap", "notifier", "patches", "landstate", "ancestry", "launchd", "beats",
           "sdnotify", "policy", "generated", "livestream", "backfillexact", "eventtrace", "statsrecords",
           "statsepisodes", "timeline", "checkrows", "runqueue", "networksignatures", "netretry", "classify",
           "causes", "eventrows", "forge", "gating", "filelog", "waitingdir", "storetoken", "inboxentry",
           "escalations", "wait", "fixersession", "fixerenv", "deletions", "testtmp", "inbox", "worktrees",
           "redrepos", "cistate", "gatesummary", "lander", "conditions", "sessionpaths", "serverclone",
           "recordingpins", "queuehold", "runverdict", "owned", "govtickets", "procwatch", "maint", "govbudget",
           "pressure", "govusage", "govfits", "queuestall", "fixerblockers", "stats", "remotejobs", "backfill",
           "store", "eventreader", "reverts", "issues", "processorlink", "timelinecmd", "batchitem", "workflowcron",
           "ciruns", "notifyurl", "redretired", "ciwhy", "redfixread", "maincontain", "landwatch", "blameopen",
           "outsideflows", "selfci", "runexplain", "runsigs", "redrecord", "leases", "owners", "statusagents",
           "hostsleep", "redfixcheck", "redask", "laptoprole", "alertroute", "alertprobe", "alerts", "ownerleases",
           "land", "explorer", "generatedregen", "conflicts", "fixerland", "remotestore", "tunnel", "storestate",
           "sessionroll", "queuefailure", "queueslots", "queue", "fixchange", "red", "queuerecheck", "redfixes",
           "redland", "culpritfix", "identitychange", "landhold", "tickservice", "projector", "statusline",
           "orphanreap", "stuckruns", "changekind", "waiting", "queuesubmit", "redfollow", "gov", "checkslot",
           "govroute", "govcmd", "fixerprompt", "escalationsession", "runowners", "redauthors", "cleanup",
           "queueuntil", "cleanuppass", "upkeep", "remote", "branches", "batchrun", "redseen", "reduntracked",
           "redblock", "cliops", "localrecheck", "queuebatch", "queuediff", "queuebatchsel", "batch",
           "workflowdispatch", "redopen", "redearly", "redclaim", "redverify", "blamerecords", "redvault", "blame",
           "redwaits", "queuecheck", "queueclose", "superseded", "park", "redcrash", "reddispatch", "fixerlife",
           "owner", "ask", "ownerwrapper", "offerdispatch", "redview", "cireport", "watchtick", "recovery",
           "netwatch", "tools", "redlease", "redpending", "redstep", "redhold", "queuepush", "orphans", "orphanpass",
           "batchpush", "batchcheck", "batchtick", "invariants", "watchdog", "landqueue", "inboxcli", "status",
           "statuspage", "redtick", "gatepublish", "serversteps", "redcli", "syncsteps", "processor", "processstore",
           "changesubmit", "queueenter", "landdryrun", "slots", "cliparser", "storemetrics", "storeapi", "storecmd",
           "ciwhycmd", "logrotate", "maintain", "cli")
