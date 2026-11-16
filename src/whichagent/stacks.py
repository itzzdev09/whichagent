"""Stack detection: [repo] install, test and suite commands for common toolchains, with per-test reports."""
import os


def detect(root):
    """[repo] settings for the stack at root, or None when it isn't recognized."""
    def has(name):
        return (root / name).exists()
    if has("uv.lock") or has("poetry.lock"):
        tool = "uv" if has("uv.lock") else "poetry"
        return {"install": "uv sync" if tool == "uv" else "poetry install",
                "test": f"{tool} run pytest -q {{files}} --junitxml={{report}}",
                "suite": f"{tool} run pytest -q --junitxml={{report}}"}
    if node := _node(root):
        return node
    if has("go.mod"):
        return {"install": "go mod download", "test": "go test -json {dirs} > {report}",
                "suite": "go test -json ./... > {report}"}
    if has("Cargo.toml"):  # per-test results come from cargo's own output
        return {"install": "cargo fetch", "test": "cargo test --no-fail-fast --test {names: --test }",
                "suite": "cargo test --no-fail-fast"}
    if has("pom.xml"):
        mvn = "mvn -q -B"
        return {"install": f"{mvn} -DskipTests test-compile",
                "test": f"{mvn} test -Dtest={{names:,}} -Dsurefire.failIfNoSpecifiedTests=false"
                        " -Dsurefire.reportsDirectory={report}",
                "suite": f"{mvn} test -Dsurefire.reportsDirectory={{report}}"}
    if has("build.gradle") or has("build.gradle.kts"):
        gradle = "gradlew" if os.name == "nt" else "./gradlew"  # cmd.exe finds gradlew.bat in the current dir
        return {"test": f"{gradle} test --tests {{names: --tests }}", "suite": f"{gradle} test",
                "report": "build/test-results/test"}
    return None


def _node(root):
    pkg = (root / "package.json").read_text("utf-8") if (root / "package.json").exists() else ""
    if (root / "package-lock.json").exists():
        install, run = "npm ci", "npx"
    elif (root / "pnpm-lock.yaml").exists():
        install, run = "pnpm install --frozen-lockfile", "pnpm exec"
    elif (root / "yarn.lock").exists():
        berry = (root / ".yarnrc.yml").exists()
        install, run = f"yarn install {'--immutable' if berry else '--frozen-lockfile'}", "yarn"
    else:
        return None
    if '"vitest"' in pkg:
        test = f"{run} vitest run {{files}} --reporter=default --reporter=junit --outputFile.junit={{report}}"
    elif '"jest"' in pkg:
        test = f"{run} jest {{files}} --json --outputFile={{report}}"
    elif "node --test" in pkg:
        test = ("node --test --test-reporter=spec --test-reporter=junit --test-reporter-destination=stdout"
                " --test-reporter-destination={report} {files}")
    else:
        return None
    return {"install": install, "test": test, "suite": test.replace(" {files}", ""), "cache": True}
