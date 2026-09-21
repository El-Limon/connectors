import java.security.MessageDigest

plugins {
    alias(libs.plugins.shadow)
}

// ---------------------------------------------------------------- the catalog target
// Every value below is written by games/zomboid/scripts/build-release.sh from
// `takaro-maint targets resolve`. The defaults keep a bare `./gradlew build` working for
// a developer, and they are deliberately not a target: an unpinned jar carries
// takaroTarget "unpinned" and is refused by `takaro-maint artifact validate`.
val takaroTarget = providers.gradleProperty("takaroTarget").getOrElse("unpinned")
val takaroFingerprint = providers.gradleProperty("takaroFingerprint").getOrElse("")
val takaroGameJar = providers.gradleProperty("takaroGameJar").getOrElse("../../_deps/projectzomboid.jar")
val takaroGameJarSha256 = providers.gradleProperty("takaroGameJarSha256").getOrElse("")
val takaroGameVersion = providers.gradleProperty("takaroGameVersion").getOrElse("unknown")
val takaroJavaRelease = providers.gradleProperty("takaroJavaRelease").getOrElse("25").toInt()
val takaroSourceRevision = providers.gradleProperty("takaroSourceRevision").getOrElse("unknown")
val takaroArtifactName = providers.gradleProperty("takaroArtifactName")
    .getOrElse("TakaroConnector-${project.version}.jar")
val takaroDepSha256 = project.properties
    .filterKeys { it.startsWith("takaroDepSha256_") }
    .mapKeys { (key, _) -> key.removePrefix("takaroDepSha256_").replace('_', '-').lowercase() }
    .mapValues { (_, value) -> value.toString() }

dependencies {
    implementation(project(":core"))
    implementation(libs.byte.buddy)
    // core's WebSocket + JSON libs are used directly by the agent (BanStore,
    // config) and must be on the agent compile + runtime classpath.
    implementation(libs.java.websocket)
    implementation(libs.gson)
    // Project Zomboid server classes — provided by the game JVM at runtime.
    // Staged by scripts/setup-environment.sh from the pinned depot manifests.
    compileOnly(files(takaroGameJar))

    testImplementation(libs.junit.jupiter)
    testRuntimeOnly(libs.junit.platform.launcher)
    testImplementation(libs.gson)
}

tasks.test {
    useJUnitPlatform()
}

// The agent links against projectzomboid.jar (class-file v69 / Java 25), which
// a --release 21 javac refuses to read. Compile the agent to 25; it only ever
// runs inside the PZ server's Java 25 JVM anyway.
tasks.withType<JavaCompile> {
    options.release = takaroJavaRelease
}

fun sha256Of(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { stream ->
        val buffer = ByteArray(1 shl 16)
        while (true) {
            val read = stream.read(buffer)
            if (read <= 0) break
            digest.update(buffer, 0, read)
        }
    }
    return digest.digest().joinToString("") { "%02x".format(it) }
}

// Nothing is compiled until the bytes this target pins are the bytes on disk. A
// mismatch here is a build failure, which `takaro-maint build` reports as exit 6.
val verifyTargetInputs by tasks.registering {
    description = "Checks the game jar and every pinned dependency against the catalog target."
    val runtimeClasspath = configurations.named("runtimeClasspath")
    doLast {
        val problems = mutableListOf<String>()
        if (takaroGameJarSha256.isNotEmpty()) {
            val jar = file(takaroGameJar)
            if (!jar.isFile) {
                problems += "$takaroGameJar is missing; run scripts/setup-environment.sh"
            } else {
                val actual = sha256Of(jar)
                if (actual != takaroGameJarSha256) {
                    problems += "${jar.name} hashes $actual, target $takaroTarget pins $takaroGameJarSha256"
                }
            }
        }
        if (takaroDepSha256.isNotEmpty()) {
            val artifacts = runtimeClasspath.get().resolvedConfiguration.resolvedArtifacts
            takaroDepSha256.forEach { (module, expected) ->
                val found = artifacts.firstOrNull { it.moduleVersion.id.name.lowercase() == module }
                if (found == null) {
                    problems += "no resolved artifact for pinned dependency '$module'"
                } else {
                    val actual = sha256Of(found.file)
                    if (actual != expected) {
                        problems += "${found.file.name} hashes $actual, target $takaroTarget pins $expected"
                    }
                }
            }
        }
        if (problems.isNotEmpty()) {
            throw GradleException("the build inputs are not the ones $takaroTarget pins:\n  " + problems.joinToString("\n  "))
        }
    }
}

tasks.named("compileJava") { dependsOn(verifyTargetInputs) }

// The identity the jar carries about itself, readable at runtime by TargetGuard and
// offline by `takaro-maint artifact validate`.
val generateTargetInfo by tasks.registering {
    description = "Writes META-INF/takaro-target.json for this target."
    val output = layout.buildDirectory.dir("generated/takaro")
    val connectorVersion = project.version.toString()
    outputs.dir(output)
    doLast {
        val file = output.get().file("META-INF/takaro-target.json").asFile
        file.parentFile.mkdirs()
        file.writeText(
            """
            {
              "target": "$takaroTarget",
              "fingerprint": "$takaroFingerprint",
              "game": "zomboid",
              "platform": "linux",
              "revision": "$takaroGameVersion",
              "connectorVersion": "$connectorVersion",
              "sourceRevision": "$takaroSourceRevision",
              "constraints": {
                "gameJarSha256": "$takaroGameJarSha256",
                "gameVersion": "$takaroGameVersion",
                "javaMin": $takaroJavaRelease
              }
            }
            """.trimIndent() + "\n",
        )
    }
}

sourceSets.main {
    resources.srcDir(generateTargetInfo)
}

tasks.shadowJar {
    // The exact name the catalog gives this target's artifact, never a glob.
    archiveFileName.set(takaroArtifactName)
    // Byte-identical wherever it is built: no timestamps, one file order, fixed modes.
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    filePermissions { unix("rw-r--r--") }
    dirPermissions { unix("rwxr-xr-x") }
    // Relocate every bundled third-party package under io.takaro.zomboid.libs
    // so nothing collides with classes the game JVM already loads.
    relocate("net.bytebuddy", "io.takaro.zomboid.libs.bytebuddy")
    relocate("org.objectweb.asm", "io.takaro.zomboid.libs.asm")
    relocate("org.java_websocket", "io.takaro.zomboid.libs.org.java_websocket")
    relocate("com.google.gson", "io.takaro.zomboid.libs.com.google.gson")
    manifest {
        attributes(
            "Premain-Class" to "io.takaro.zomboid.agent.TakaroAgent",
            "Agent-Class" to "io.takaro.zomboid.agent.TakaroAgent",
            "Can-Retransform-Classes" to "true",
            "Can-Redefine-Classes" to "true",
            "Implementation-Title" to "Takaro Project Zomboid Connector",
            "Implementation-Version" to project.version.toString(),
            "ByteBuddy-Version" to libs.versions.byte.buddy.get(),
            "Takaro-Target" to takaroTarget,
            "Takaro-Target-Fingerprint" to takaroFingerprint,
            "Takaro-Connector-Version" to project.version.toString(),
            "Takaro-Source-Revision" to takaroSourceRevision,
            "Takaro-Game-Version" to takaroGameVersion,
            "Takaro-Java-Release" to takaroJavaRelease.toString(),
        )
    }
}

tasks.build {
    dependsOn(tasks.shadowJar)
}
