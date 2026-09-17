plugins {
    `kotlin-dsl`
}

repositories {
    mavenCentral()
    gradlePluginPortal()
}

// buildSrc deliberately carries no game plugins (Loom, shadow, ModDev). Putting one here
// would place it on every project's buildscript classpath with an unknown version, and the
// modules that still declare it with a version could no longer resolve it. The conventions
// below therefore only use Gradle's own types; a target applies its loader plugins itself.
dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter:6.1.0")
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
}
