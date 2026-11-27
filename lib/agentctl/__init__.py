


"""agentctl's code: real modules (2026-11-22, the owner: "make the parts real modules").

Each module imports the standard library and the modules before it in MODULES, explicitly, and nothing else: a name
is the module's own or imported. The entry (~/bin/agentctl) imports cli, the composition root: it wires what a module
must not import (`CurrentStore(process_store)`, the condition registry, ...) and holds main. cli's imports reach every module.

MODULES is every module in import order, each after every module it imports (devtools/placemodules.py writes it): the
manifest the quality tools (quality/typecheck.py) and the tests' view of the package (tests/agentctl_view.py) read.
"""
MODULES = ("procrunner", "conditionregistry", "escalationtable", "psparse", "govpool", "statefile", "selfcode",
           "statemachines", "storecalls", "clock", "filelock", "ids", "timefmt", "livepage", "holdgraph",
           "configfile", "knobread", "watchknobs", "tickknobs", "storeknobs", "storeauth", "redknobs", "blameknobs",
           "queueknobs", "govknobs", "fixerknobs", "leasetable", "offers", "cpu", "core", "texthash", "percentile",
           "tokenerrors", "landedtwin", "ghapp", "ghtally", "repos", "buildview", "config", "workflows", "trailers",
           "claudefiles", "allowedpaths", "procs", "fixerrecords", "actors", "live", "clones", "machine", "jvmreap",
           "notifier", "launchd", "beats", "sdnotify", "policy", "generated", "livestream", "backfillexact",
           "eventtrace", "statsrecords", "statsepisodes", "timeline", "checkrows", "networksignatures", "netretry",
           "classify", "causes", "eventrows", "forge", "gating", "filelog", "waitingdir", "inboxentry",
           "escalations", "joblog", "hookoutput", "deletions", "inbox", "redrepos", "cistate", "gatesummary",
           "sessionpaths", "runverdict", "owned", "govtickets", "maint", "govbudget", "pressure", "govusage",
           "govfits", "stats", "checkrunjobs", "backfill", "store", "eventreader", "timelinecmd", "workflowcron",
           "ciruns", "context", "notifyurl", "redretired", "ciwhy", "redfixread", "outsideflows", "queuefailure",
           "fixchange", "landhold", "logrotate", "onmain", "worktrees", "fixerblockers", "landwatch", "runowners",
           "redauthors", "cleanup", "queueuntil", "cleanuppass", "workflowdispatch", "entryfiles",
           "serverhousekeeping", "agewords", "runqueue", "storetoken", "fixersession", "fixerenv", "testtmp",
           "lander", "conditions", "serverclone", "recordingpins", "queuehold", "procwatch", "queuestall",
           "processorlink", "batchitem", "blameopen", "selfci", "runexplain", "runsigs", "redrecord", "leases",
           "owners", "statusagents", "hostsleep", "redfixcheck", "redask", "laptoprole", "alertroute", "ownerleases",
           "land", "generatedregen", "remotestore", "tunnel", "storestate", "red", "tickservice", "statusline",
           "stuckruns", "processstore", "reverts", "issues", "alertclear", "alerts", "explorer", "conflicts",
           "fixerland", "sessionroll", "queueslots", "queue", "queuerecheck", "redfixes", "redland", "culpritfix",
           "identitychange", "eventprojection", "orphanreap", "changekind", "waiting", "queuesubmit", "gov",
           "checkslot", "govroute", "govcmd", "fixerprompt", "escalationsession", "upkeep", "checkrun", "branches",
           "batchrun", "redseen", "reduntracked", "redblock", "cliops", "localrecheck", "redopen", "redearly",
           "redclaim", "redverify", "blamerecords", "redvault", "blame", "redwaits", "redcrash", "reddispatch",
           "fixerlife", "owner", "ask", "ownerwrapper", "crashloop", "offerdispatch", "redview", "cireport",
           "watchtick", "recovery", "netwatch", "tools", "redlease", "redpending", "redstep", "redhold", "redtick",
           "gatepublish", "changesubmit", "storemetrics", "storeapi", "storecmd", "ciwhycmd", "inboxwait",
           "inboxcli", "landedchange", "superseded", "park", "orphans", "orphanpass", "maintain", "status",
           "statuspage", "redcli", "changerows", "changelap", "overlapcheck", "entrychecks", "queuediff",
           "batchselect", "batch", "syncsteps", "invariants", "watchdog", "queueinfra", "queuecheck", "checkround",
           "queuepush", "batchpush", "batchcheck", "batchtick", "serversteps", "landqueue", "processor",
           "queueenter", "landdryrun", "slots", "cliparser", "cli")
