import io.takaro.gradle.SourceRevision
import io.takaro.gradle.TargetCatalog
import java.security.MessageDigest

plugins {
    id("takaro.java-conventions")
}

// Everything below is derived from one catalog record. Nothing here selects a version.
val target = TargetCatalog.load(project)
val repoRoot = TargetCatalog.repoRoot(project)
val sourceRevision = SourceRevision.of(
    repoRoot,
    listOf("games/minecraft", "catalog/minecraft"),
    findProperty("takaroSourceRevision")?.toString()
)

extra["takaroTarget"] = target
extra["takaroSourceRevision"] = sourceRevision

base {
    archivesName.set("takaro-minecraft-mod-${target.id}")
}

java {
    toolchain {
        languageVersion.set(JavaLanguageVersion.of(target.javaRelease))
    }
}

tasks.withType<JavaCompile>().configureEach {
    options.release.set(target.javaRelease)
}

// The identity a running server reads back out of the jar.
val generatedResources = layout.buildDirectory.dir("generated/takaro")

val generateTargetInfo by tasks.registering {
    val outputDir = generatedResources
    val recordFile = target.file
    val projectVersion = project.version.toString()
    inputs.file(recordFile)
    inputs.property("version", projectVersion)
    inputs.property("sourceRevision", sourceRevision)
    outputs.dir(outputDir)
    doLast {
        val loader = target.inputs["loader"]
        @Suppress("UNCHECKED_CAST")
        val loaderMap = loader as? Map<String, Any?>
        val info = linkedMapOf<String, Any?>(
            "target" to target.id,
            "fingerprint" to target.fingerprint,
            "game" to target.game,
            "platform" to target.platform,
            "revision" to target.revision,
            "connectorVersion" to projectVersion,
            "sourceRevision" to sourceRevision,
            "constraints" to linkedMapOf(
                "gameVersion" to target.revision,
                "loaderMin" to (loaderMap?.get("loaderVersion") ?: ""),
                "javaMin" to target.javaRelease
            )
        )
        val file = outputDir.get().file("META-INF/takaro-target.json").asFile
        file.parentFile.mkdirs()
        file.writeText(TargetCatalog.canonicalJson(info) + "\n")
    }
}

sourceSets.named("main") {
    resources.srcDir(generateTargetInfo.map { generatedResources })
}

// Placeholders the loader manifests share; a platform that does not use a key gets "".
@Suppress("UNCHECKED_CAST")
val loaderInput = target.inputs["loader"] as? Map<String, Any?>

@Suppress("UNCHECKED_CAST")
val fabricApiInput = target.inputs["fabricApi"] as? Map<String, Any?>

val expansions = mapOf(
    "version" to project.version.toString(),
    "target_id" to target.id,
    "mc_version" to target.revision,
    "loader_min" to (loaderInput?.get("loaderVersion")?.toString() ?: ""),
    "fabric_api_min" to (fabricApiInput?.get("version")?.toString() ?: ""),
    "java_min" to target.javaRelease.toString(),
    "neoforge_range" to "",
    "paper_api_version" to ""
)

tasks.withType<ProcessResources>().configureEach {
    inputs.properties(expansions)
    filesMatching(listOf("plugin.yml", "fabric.mod.json", "META-INF/neoforge.mods.toml")) {
        expand(expansions)
    }
}

val takaroManifestAttributes = mapOf(
    "Takaro-Target" to target.id,
    "Takaro-Target-Fingerprint" to target.fingerprint,
    "Takaro-Connector-Version" to project.version.toString(),
    "Takaro-Source-Revision" to sourceRevision,
    "Takaro-Game-Version" to target.revision,
    "Takaro-Java-Release" to target.javaRelease.toString()
)

tasks.withType<Jar>().configureEach {
    manifest {
        attributes(takaroManifestAttributes)
    }
}

/**
 * Resolves every build dependency the catalog pins a hash for and compares the bytes.
 * A poisoned or re-published artifact fails the build instead of shipping.
 */
val verifyTargetInputs by tasks.registering {
    val hashed = target.deps.filterValues { it["sha256"] != null }
    val coordinates = hashed.mapValues { it.value["coordinate"] as String }
    val expected = hashed.mapValues { it.value["sha256"] as String }
    inputs.property("coordinates", coordinates)
    inputs.property("expected", expected)
    val outputFile = layout.buildDirectory.file("takaro/verified-inputs.txt")
    outputs.file(outputFile)
    doLast {
        val problems = mutableListOf<String>()
        val lines = mutableListOf<String>()
        for ((name, coordinate) in coordinates.entries.sortedBy { it.key }) {
            val detached = configurations.detachedConfiguration(dependencies.create(coordinate))
            detached.isTransitive = false
            val files = detached.resolve()
            if (files.isEmpty()) {
                problems += "$name ($coordinate): nothing resolved"
                continue
            }
            for (file in files.sortedBy { it.name }) {
                val digest = MessageDigest.getInstance("SHA-256")
                    .digest(file.readBytes())
                    .joinToString("") { String.format("%02x", it) }
                if (digest != expected[name]) {
                    problems += "$name (${file.name}): expected ${expected[name]} actual $digest"
                } else {
                    lines += "$name ${file.name} $digest"
                }
            }
        }
        if (problems.isNotEmpty()) {
            throw GradleException("pinned build inputs do not match the catalog:\n  " + problems.joinToString("\n  "))
        }
        val file = outputFile.get().asFile
        file.parentFile.mkdirs()
        file.writeText(lines.joinToString("\n") + "\n")
    }
}

tasks.named("compileJava") {
    dependsOn(verifyTargetInputs)
}
