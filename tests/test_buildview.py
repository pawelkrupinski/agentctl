"""BUILD-AWARE re-checks (buildview.py, the owner 2026-11-17: a build.sbt / project/ change landing meanwhile forced a full
re-check of every queued change, most of them refactor churn): the build as the change's modules see it -- the
build-wide statements, their project definitions with everything those name (settings helpers, configurations, the
projects they dependsOn), the project/ members they use -- compared at the full check's tip and at the rebased tip."""
import os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_agentctl import A, sh  # noqa: E402

BUILD = """import Dependencies._

// the build-wide settings
ThisBuild / scalacOptions ++= Seq("-feature")

lazy val shared = Seq(libraryDependencies += jsoup)
lazy val forkTests = Seq(Test / fork := true)

lazy val common = (project in file("common"))
  .settings(shared)

lazy val web = (project in file("web"))
  .dependsOn(common)
  .settings(libraryDependencies += play)
  .settings(Forked.settings(Test))

lazy val worker = (project in file("worker"))
  .dependsOn(common)
  .settings(libraryDependencies += caffeine, forkTests)

addCommandAlias("testWorker", "worker/Test/test")
"""

DEPENDENCIES = """import sbt._

object Dependencies {
  private val playVersion = "3.0.1"
  val play     = "org.playframework" %% "play" % playVersion
  val caffeine = "com.github.ben-manes.caffeine" % "caffeine" % "3.3.1"
  val jsoup    = "org.jsoup" % "jsoup" % "1.24.1"
}
"""

FORKED = """import sbt._

object Forked {
  def settings(config: Configuration) = Seq(config / fork := true, heap)
  private def heap = "512m"
  def slots = 4
}
"""


class BuildViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        sh(self.repo, "git", "init", "-q")
        self.write("build.sbt", BUILD)
        self.write("project/Dependencies.scala", DEPENDENCIES)
        self.write("project/Forked.scala", FORKED)
        self.write("project/build.properties", "sbt.version=1.11.0\n")
        self.full = self.commit("the build")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text):
        os.makedirs(os.path.dirname(os.path.join(self.repo, path)) or self.repo, exist_ok=True)
        with open(os.path.join(self.repo, path), "w") as f:
            f.write(text)

    def commit(self, msg):
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "commit", "-q", "-m", msg)
        return sh(self.repo, "git", "rev-parse", "HEAD").strip()

    def judged(self, path, old, new, paths=("web/src/main/A.scala",), selection=None):
        """What the rule says of a change to `paths` after `path` had old replaced by new on main."""
        text = open(os.path.join(self.repo, path)).read()
        self.assertIn(old, text)
        self.write(path, text.replace(old, new))
        tip = self.commit(f"{path} moves")
        return A.build_view_change(self.repo, self.full, tip, list(paths), selection or {})

    def test_another_modules_settings_leave_the_view_alone(self):
        self.assertIsNone(self.judged("build.sbt", "libraryDependencies += caffeine, forkTests",
                                      "libraryDependencies += caffeine"))

    def test_a_comment_or_a_new_module_leaves_the_view_alone(self):
        self.assertIsNone(self.judged("build.sbt", "// the build-wide settings",
                                      "// build-wide\nlazy val extra = (project in file(\"extra\"))\n"))

    def test_an_unused_dependency_bump_leaves_the_view_alone(self):
        self.assertIsNone(self.judged("project/Dependencies.scala", '"caffeine" % "3.3.1"', '"caffeine" % "3.4.0"'))

    def test_an_unused_project_member_leaves_the_view_alone(self):
        self.assertIsNone(self.judged("project/Forked.scala", "def slots = 4", "def slots = 8"))

    def test_the_modules_own_definition_changes_it(self):
        self.assertIn("web", self.judged("build.sbt", "libraryDependencies += play", "libraryDependencies ++= Seq(play)"))

    def test_a_dependsOn_edge_changes_it(self):
        self.assertIsNotNone(self.judged("build.sbt", ".dependsOn(common)\n  .settings(libraryDependencies += play)",
                                         ".dependsOn(common, worker)\n  .settings(libraryDependencies += play)"))

    def test_an_upstream_modules_settings_change_it(self):
        """web dependsOn common: common's settings helper (and the dependency it names) are web's too."""
        self.assertIn("shared", self.judged("build.sbt", "libraryDependencies += jsoup", "libraryDependencies += play"))

    def test_a_used_dependency_version_changes_it(self):
        """play's version is a private member play names: a bump reaches web."""
        self.assertIn("Dependencies.playVersion",
                      self.judged("project/Dependencies.scala", '"3.0.1"', '"3.0.2"'))

    def test_a_used_project_helper_changes_it(self):
        """Forked.settings names heap: a member it uses moves the view."""
        self.assertIn("Forked.heap", self.judged("project/Forked.scala", '"512m"', '"1g"'))

    def test_a_build_wide_setting_changes_every_view(self):
        self.assertIn("a build-wide setting", self.judged("build.sbt", '"-feature"', '"-feature", "-Werror"',
                                                          paths=("docs/x.md",)))

    def test_any_other_project_file_changes_every_view(self):
        self.assertIn("build.properties", self.judged("project/build.properties", "1.11.0", "1.11.1",
                                                      paths=("docs/x.md",)))

    def test_an_auto_plugin_on_every_project_changes_every_view(self):
        self.write("project/Tripwire.scala", "import sbt._\nobject Tripwire extends AutoPlugin {\n"
                                             "  override def trigger = allRequirements\n  val x = 1\n}\n")
        self.full = self.commit("an auto plugin")
        self.assertIsNotNone(self.judged("project/Tripwire.scala", "val x = 1", "val x = 2", paths=("docs/x.md",)))

    def test_an_auto_plugin_with_a_typed_trigger_changes_every_view(self):
        """Review 2026-11-18: `override def trigger: PluginTrigger = allRequirements` was read as no auto plugin."""
        self.write("project/Tripwire.scala", "import sbt._\nobject Tripwire extends AutoPlugin {\n"
                                             "  override def trigger: PluginTrigger = allRequirements\n  val x = 1\n}\n")
        self.full = self.commit("an auto plugin")
        self.assertIsNotNone(self.judged("project/Tripwire.scala", "val x = 1", "val x = 2", paths=("docs/x.md",)))

    def test_another_root_sbt_file_is_the_build_too(self):
        """Review 2026-11-18: sbt reads every root *.sbt (movies' coupling.sbt and identity-impact.sbt: the tasks the
        hook's coupling ratchet and identity narrowing run). One landing meanwhile was no build change at all."""
        self.write("identity-impact.sbt", "lazy val impactUnits = Def.task { Seq(1) }\n")
        self.full = self.commit("a second root sbt file")
        self.assertTrue(A.forces_full_recheck("identity-impact.sbt"))
        self.assertIsNotNone(A.BUILD_VIEW_PATHS.match("identity-impact.sbt"))
        self.assertIn("identity-impact.sbt", self.judged("identity-impact.sbt", "Seq(1)", "Seq(2)", paths=("docs/x.md",)))
        self.assertIn("the change itself", A.build_view_change(self.repo, self.full, self.full, ["coupling.sbt"], {}))

    def test_the_modules_the_selection_names_are_seen_too(self):
        """A docs change whose selection runs worker's tests (an area line): worker's definition is in its view."""
        moves = ("build.sbt", "libraryDependencies += caffeine, forkTests", "libraryDependencies += caffeine")
        self.assertIn("worker", self.judged(*moves, paths=("docs/x.md",),
                                            selection={"--plan": ["sbt"], "local-tests": ["basic sbt worker/testOnly x"]}))

    def test_the_compile_and_lint_plans_name_no_module(self):
        """The verification compiles every test config and runs the lints, under the new build: they seed nothing."""
        self.assertIsNone(self.judged("build.sbt", "libraryDependencies += caffeine, forkTests",
                                      "libraryDependencies += caffeine", paths=("docs/x.md",),
                                      selection={"--compile-plan": ["worker/Test/compile"],
                                                 "--lint-plan": ["worker:tools.LintSpec"]}))

    def test_a_project_file_a_module_compiles_by_name_is_in_its_view(self):
        """`Test / unmanagedSources ++= Seq("Forked.scala")`: the file is the module's test source."""
        self.write("build.sbt", BUILD.replace(".settings(shared)", '.settings(shared, Test / unmanagedSources ++= '
                                                                  'Seq("Forked.scala"))'))
        self.full = self.commit("common compiles Forked.scala")
        self.assertIn("project/Forked.scala", self.judged("project/Forked.scala", "def slots = 4", "def slots = 8"))

    def test_a_change_to_the_build_itself_is_judged_whole(self):
        self.assertIn("the change itself", self.judged("build.sbt", "// the build-wide settings", "// build-wide",
                                                       paths=("project/Forked.scala",)))

    def test_an_unreadable_build_or_an_unknown_tip_is_no_answer(self):
        self.assertIn("unknown", A.build_view_change(self.repo, "f" * 40, self.full, ["web/src/A.scala"], {}))
        sh(self.repo, "git", "rm", "-q", "build.sbt")
        tip = self.commit("no build")
        self.assertIn("cannot be read", A.build_view_change(self.repo, self.full, tip, ["web/src/A.scala"], {}))

    def test_the_comment_strip_keeps_strings(self):
        self.assertEqual(A.scala_code('val x = "a // b" // c\n/* d */val y = 1'), 'val x = "a // b" \nval y = 1')

    def test_the_comment_strip_keeps_triple_quoted_strings_and_ends_at_an_unterminated_one(self):
        self.assertEqual(A.scala_code('a """x // y\n/* z */""" b // c'), 'a """x // y\n/* z */""" b ')
        self.assertEqual(A.scala_code('a "open // c\nb'), 'a "open // c\nb')
        self.assertEqual(A.scala_code('a /* never closed'), 'a ')
        self.assertEqual(A.scala_code('a "esc \\" // k" // c'), 'a "esc \\" // k" ')


if __name__ == "__main__":
    unittest.main()
