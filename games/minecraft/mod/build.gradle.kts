plugins {
    java
}

val versionFromFile = rootProject.file("version.txt").takeIf { it.exists() }?.readText()?.trim()

allprojects {
    version = findProperty("version")?.toString()?.takeIf { it != "unspecified" }
        ?: versionFromFile
        ?: "1.0.0-SNAPSHOT"
}

subprojects {
    // Target projects configure themselves from their catalog record; this block only
    // covers the legacy modules that keep the Java 21 toolchain until they are migrated.
    if (projectDir.parentFile.name == "targets") return@subprojects

    apply(plugin = "java")

    base {
        archivesName = "takaro-${project.name}"
    }

    java {
        sourceCompatibility = JavaVersion.VERSION_21
        targetCompatibility = JavaVersion.VERSION_21
    }

    repositories {
        mavenCentral()
    }

    tasks.withType<JavaCompile> {
        options.encoding = "UTF-8"
        options.release = 21
    }

    tasks.withType<ProcessResources> {
        filesMatching(listOf("plugin.yml", "fabric.mod.json", "META-INF/neoforge.mods.toml")) {
            expand("version" to project.version)
        }
    }
}
