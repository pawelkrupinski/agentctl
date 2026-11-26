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
           "leasetable", "offers", "cpu", "core", "ghapp", "ghtally", "repos", "buildview", "config", "workflows",
           "trailers", "claudefiles", "allowedpaths", "procs", "fixerrecords", "actors", "live", "clones", "machine",
           "jvmreap", "notifier", "patches", "landstate", "redworkflows", "ancestry", "launchd", "beats", "sdnotify",
           "policy", "redrecord", "leases", "generated", "owners", "livestream", "statusagents", "backfillexact",
           "hostsleep", "eventtrace", "statsrecords", "statsepisodes", "timeline", "checkrows", "runqueue",
           "networksignatures", "netretry", "classify", "causes", "eventrows", "forge", "notifyurl", "redretired",
           "gating", "filelog", "waitingdir", "storetoken", "inboxentry", "escalations", "wait", "fixersession",
           "fixerenv", "deletions", "testtmp", "inbox", "worktrees", "redrepos", "redfamilies", "cistate", "ciwhy",
           "gatesummary", "lander", "redfixread", "maincontain", "redfixcheck", "conditions", "redlink", "redask",
           "laptoprole", "red", "landwatch", "blameopen", "sessionpaths", "serverclone", "recordingpins",
           "outsideflows", "alertroute", "alertprobe", "alerts", "ownerleases", "queuehold", "runverdict", "owned",
           "govtickets", "procwatch", "maint", "govbudget", "selfci", "pressure", "govusage", "govfits",
           "queuestall", "cidispatches", "rundispatch", "redauthors", "cleanup", "upkeep", "queueuntil",
           "cleanuppass", "fixerblockers", "stats", "remotejobs", "backfill", "store", "eventreader", "land",
           "explorer", "reverts", "generatedregen", "conflicts", "redreplay", "issues", "fixerland", "remotestore",
           "tunnel", "storestate", "culpritfix", "sessionroll", "queuefailure", "queueslots", "queue", "fixchange",
           "queuerecheck", "redfixes", "redland", "identitychange", "fixer", "redmove", "landhold", "processorlink",
           "tickservice", "timelinecmd", "projector", "statusline", "remote", "escalationsession", "orphanreap",
           "runexplain", "runsigs", "stuckruns", "redepisode", "branches", "changekind", "waiting", "queuesubmit",
           "redfollow", "gov", "checkslot", "govroute", "govcmd", "batchrun", "redseen", "redobserve",
           "reduntracked", "redblock", "redopen", "redearly", "redclaim", "redverify", "blamerecords", "redvault",
           "redownerpush", "redquota", "blame", "redwaits", "cliops", "localrecheck", "queuebatch", "queuediff",
           "queuecheck", "queueclose", "superseded", "park", "redcrash", "reddispatch", "reddecision", "fixerlife",
           "owner", "ask", "ownerwrapper", "offerdispatch", "redview", "cireport", "watchtick", "batchitem",
           "recovery", "netwatch", "tools", "redlease", "redpending", "redstep", "redhold", "server",
           "queuebatchsel", "batch", "queuepush", "orphans", "orphanpass", "batchpush", "batchcheck", "batchtick",
           "maintain", "invariants", "watchdog", "landqueue", "inboxcli", "status", "statuspage", "redstatus",
           "syncsteps", "queueenter", "landdryrun", "slots", "cliparser", "redtick", "serversteps", "processor",
           "cli", "gatepublish", "redcli")
