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
           "clones", "machine", "jvmreap", "notifier", "launchd", "beats", "sdnotify", "policy", "generated",
           "livestream", "backfillexact", "eventtrace", "statsrecords", "statsepisodes", "timeline", "checkrows",
           "runqueue", "networksignatures", "netretry", "classify", "causes", "eventrows", "forge", "gating",
           "filelog", "waitingdir", "storetoken", "inboxentry", "escalations", "joblog", "hookoutput",
           "fixersession", "fixerenv", "deletions", "testtmp", "inbox", "redrepos", "cistate", "gatesummary",
           "lander", "conditions", "sessionpaths", "serverclone", "recordingpins", "queuehold", "runverdict",
           "owned", "govtickets", "procwatch", "maint", "govbudget", "pressure", "govusage", "govfits", "queuestall",
           "stats", "remotejobs", "backfill", "store", "eventreader", "processorlink", "timelinecmd", "batchitem",
           "workflowcron", "ciruns", "notifyurl", "redretired", "ciwhy", "redfixread", "blameopen", "outsideflows",
           "selfci", "runexplain", "runsigs", "redrecord", "leases", "owners", "statusagents", "hostsleep",
           "redfixcheck", "redask", "laptoprole", "alertroute", "ownerleases", "land", "generatedregen",
           "remotestore", "tunnel", "storestate", "queuefailure", "fixchange", "red", "landhold", "tickservice",
           "statusline", "stuckruns", "processstore", "logrotate", "onmain", "worktrees", "fixerblockers", "reverts",
           "issues", "landwatch", "alertprobe", "alerts", "explorer", "conflicts", "fixerland", "sessionroll",
           "queueslots", "queue", "queuerecheck", "redfixes", "redland", "culpritfix", "identitychange", "projector",
           "orphanreap", "changekind", "waiting", "queuesubmit", "redfollow", "gov", "checkslot", "govroute",
           "govcmd", "fixerprompt", "escalationsession", "runowners", "redauthors", "cleanup", "queueuntil",
           "cleanuppass", "upkeep", "remote", "branches", "batchrun", "redseen", "reduntracked", "redblock",
           "cliops", "localrecheck", "queuebatch", "queuediff", "queuebatchsel", "batch", "workflowdispatch",
           "redopen", "redearly", "redclaim", "redverify", "blamerecords", "redvault", "blame", "redwaits",
           "queuecheck", "redcrash", "reddispatch", "fixerlife", "owner", "ask", "ownerwrapper", "offerdispatch",
           "redview", "cireport", "watchtick", "recovery", "netwatch", "tools", "redlease", "redpending", "redstep",
           "redhold", "redtick", "gatepublish", "changesubmit", "storemetrics", "storeapi", "storecmd", "ciwhycmd",
           "inboxwait", "inboxcli", "landedchange", "superseded", "park", "orphans", "orphanpass", "maintain",
           "status", "statuspage", "redcli", "syncsteps", "changerows", "invariants", "watchdog", "changelap",
           "queuepush", "batchpush", "batchcheck", "batchtick", "serversteps", "checkround", "landqueue",
           "processor", "queueenter", "landdryrun", "slots", "cliparser", "cli")
