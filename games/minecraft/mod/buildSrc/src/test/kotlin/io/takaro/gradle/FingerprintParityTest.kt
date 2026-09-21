package io.takaro.gradle

import groovy.json.JsonSlurper
import java.io.File
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertNotEquals
import org.junit.jupiter.api.Test

/**
 * Gradle and the maintenance command must agree on every fingerprint, or a build would
 * stamp a jar with an identity the deploy side refuses. The fixture is written by the
 * Python implementation; this test proves the Kotlin twin reproduces it byte for byte.
 */
class FingerprintParityTest {

    private fun fixtureFile(): File {
        // buildSrc/ -> mod/ -> minecraft/ -> games/ -> repo root
        var dir = File(System.getProperty("user.dir")).absoluteFile
        repeat(8) {
            val candidate = File(dir, "maintenance/tests/fixtures/fingerprints.json")
            if (candidate.isFile) return candidate
            dir = dir.parentFile ?: return@repeat
        }
        throw IllegalStateException("could not find maintenance/tests/fixtures/fingerprints.json")
    }

    @Suppress("UNCHECKED_CAST")
    private fun cases(): List<Map<String, Any?>> =
        JsonSlurper().parse(fixtureFile()) as List<Map<String, Any?>>

    @Test
    fun `every fixture case fingerprints the same in Gradle`() {
        val cases = cases()
        check(cases.size >= 4) { "the parity fixture should cover at least four cases" }
        for (case in cases) {
            @Suppress("UNCHECKED_CAST")
            val record = case["record"] as Map<String, Any?>
            assertEquals(
                case["fingerprint"] as String,
                TargetCatalog.fingerprint(record),
                "fingerprint mismatch for '${case["name"]}'"
            )
        }
    }

    @Test
    fun `key order does not change a fingerprint`() {
        val byName = cases().associateBy { it["name"] as String }
        val sorted = byName.getValue("sorted twin")
        val reordered = byName.getValue("the same record with every key reordered")

        @Suppress("UNCHECKED_CAST")
        val a = TargetCatalog.fingerprint(sorted["record"] as Map<String, Any?>)

        @Suppress("UNCHECKED_CAST")
        val b = TargetCatalog.fingerprint(reordered["record"] as Map<String, Any?>)

        assertEquals(a, b)
    }

    @Test
    fun `a different container digest changes the fingerprint`() {
        val byName = cases().associateBy { it["name"] as String }

        @Suppress("UNCHECKED_CAST")
        val real = TargetCatalog.fingerprint(
            byName.getValue("minecraft/fabric-26.2 (the real record)")["record"] as Map<String, Any?>
        )

        @Suppress("UNCHECKED_CAST")
        val changed = TargetCatalog.fingerprint(
            byName.getValue("fabric-26.2 with a different container digest")["record"] as Map<String, Any?>
        )

        assertNotEquals(real, changed)
    }

    @Test
    fun `support notes and the default flag stay outside the fingerprint`() {
        val byName = cases().associateBy { it["name"] as String }

        @Suppress("UNCHECKED_CAST")
        val real = TargetCatalog.fingerprint(
            byName.getValue("minecraft/fabric-26.2 (the real record)")["record"] as Map<String, Any?>
        )

        @Suppress("UNCHECKED_CAST")
        val annotated = TargetCatalog.fingerprint(
            byName.getValue("fabric-26.2 with different support notes and default flag")["record"]
                as Map<String, Any?>
        )

        assertEquals(real, annotated)
    }
}
