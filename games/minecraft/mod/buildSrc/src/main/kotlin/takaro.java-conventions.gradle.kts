plugins {
    java
}

repositories {
    mavenCentral()
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
}

// Reproducible archives: two builds of the same sources must produce the same bytes,
// so a release can be rebuilt and compared instead of trusted.
tasks.withType<AbstractArchiveTask>().configureEach {
    isPreserveFileTimestamps = false
    isReproducibleFileOrder = true
    filePermissions { unix("644") }
    dirPermissions { unix("755") }
}
