package io.takaro.gradle

import java.io.File

/** The commit a build came from, and whether the sources it covers were edited. */
object SourceRevision {

    /**
     * A caller that already knows the revision (the maintenance command, or CI) passes it in:
     * container toolchains have neither git nor the worktree metadata, and a build that guesses
     * would stamp every jar "unknown".
     */
    fun of(repoRoot: File, watched: List<String>, override: String? = null): String {
        if (!override.isNullOrBlank()) return override
        val head = git(repoRoot, listOf("rev-parse", "HEAD")) ?: return "unknown"
        val status = git(repoRoot, listOf("status", "--porcelain", "--") + watched).orEmpty()
        return if (status.isBlank()) head else head + "-dirty"
    }

    private fun git(workingDir: File, args: List<String>): String? = try {
        val process = ProcessBuilder(listOf("git") + args)
            .directory(workingDir)
            .redirectErrorStream(true)
            .start()
        val output = process.inputStream.bufferedReader().readText().trim()
        if (process.waitFor() == 0) output else null
    } catch (_: Exception) {
        null
    }
}
