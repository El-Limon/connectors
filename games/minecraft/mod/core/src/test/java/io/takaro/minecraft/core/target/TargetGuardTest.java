package io.takaro.minecraft.core.target;

import org.junit.jupiter.api.Test;

import java.util.Optional;

import static org.junit.jupiter.api.Assertions.*;

class TargetGuardTest {

    private static TargetInfo fabric262() {
        return TargetInfo.of("fabric-26.2", "a".repeat(64), "minecraft", "fabric", "26.2",
                "0.1.1", "abc123", "26.2", "0.19.5", 25);
    }

    private static RuntimeIdentity matching() {
        return new RuntimeIdentity("26.2", "fabric", "0.19.5", 25);
    }

    @Test
    void exactMatchIsOk() {
        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), matching(), TargetGuard.Policy.ENFORCE);

        assertEquals("ok", decision.result());
        assertTrue(decision.connect());
        assertTrue(decision.reasons().isEmpty());
    }

    @Test
    void differentGameVersionIsRefused() {
        RuntimeIdentity other = new RuntimeIdentity("26.1.2", "fabric", "0.19.5", 25);

        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), other, TargetGuard.Policy.ENFORCE);

        assertEquals("refuse", decision.result());
        assertFalse(decision.connect());
        assertTrue(decision.reasons().get(0).contains("26.1.2"));
    }

    @Test
    void loaderBelowTheMinimumIsRefused() {
        RuntimeIdentity older = new RuntimeIdentity("26.2", "fabric", "0.19.3", 25);

        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), older, TargetGuard.Policy.ENFORCE);

        assertEquals("refuse", decision.result());
        assertTrue(decision.reasons().get(0).contains("loader 0.19.5"));
    }

    @Test
    void loaderAboveTheMinimumIsAccepted() {
        RuntimeIdentity newer = new RuntimeIdentity("26.2", "fabric", "0.20.0", 25);

        assertEquals("ok",
                TargetGuard.evaluate(Optional.of(fabric262()), newer, TargetGuard.Policy.ENFORCE).result());
    }

    @Test
    void javaBelowTheMinimumIsRefused() {
        RuntimeIdentity old = new RuntimeIdentity("26.2", "fabric", "0.19.5", 21);

        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), old, TargetGuard.Policy.ENFORCE);

        assertEquals("refuse", decision.result());
        assertTrue(decision.reasons().get(0).contains("Java 25"));
    }

    @Test
    void missingTargetInfoIsUnchecked() {
        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.empty(), matching(), TargetGuard.Policy.ENFORCE);

        assertEquals("unchecked", decision.result());
        assertTrue(decision.connect());
    }

    @Test
    void missingRuntimeIdentityIsUnchecked() {
        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), null, TargetGuard.Policy.ENFORCE);

        assertEquals("unchecked", decision.result());
        assertTrue(decision.connect());
    }

    @Test
    void policyOffSkipsTheCheckEntirely() {
        RuntimeIdentity wrong = new RuntimeIdentity("1.21.11", "fabric", "0.1.0", 17);

        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), wrong, TargetGuard.Policy.OFF);

        assertEquals("unchecked", decision.result());
        assertTrue(decision.connect());
    }

    @Test
    void warnPolicyRefusesButStillConnects() {
        RuntimeIdentity wrong = new RuntimeIdentity("26.1.2", "fabric", "0.19.5", 25);

        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), wrong, TargetGuard.Policy.WARN);

        assertEquals("refuse", decision.result());
        assertTrue(decision.connect());
    }

    @Test
    void enforcePolicyRefusesAndDoesNotConnect() {
        RuntimeIdentity wrong = new RuntimeIdentity("26.1.2", "fabric", "0.19.5", 25);

        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), wrong, TargetGuard.Policy.ENFORCE);

        assertEquals("refuse", decision.result());
        assertFalse(decision.connect());
    }

    @Test
    void policyParsingDefaultsToEnforce() {
        assertEquals(TargetGuard.Policy.ENFORCE, TargetGuard.Policy.parse(null));
        assertEquals(TargetGuard.Policy.ENFORCE, TargetGuard.Policy.parse(""));
        assertEquals(TargetGuard.Policy.ENFORCE, TargetGuard.Policy.parse("nonsense"));
        assertEquals(TargetGuard.Policy.WARN, TargetGuard.Policy.parse(" WARN "));
        assertEquals(TargetGuard.Policy.OFF, TargetGuard.Policy.parse("off"));
        assertEquals(TargetGuard.Policy.OFF, TargetGuard.Policy.parse("0"));
    }

    @Test
    void versionComparisonOrdersDottedIntegers() {
        assertTrue(TargetGuard.compareVersions("0.19.3", "0.19.5") < 0);
        assertTrue(TargetGuard.compareVersions("0.19.5", "0.20.0") < 0);
        assertTrue(TargetGuard.compareVersions("0.20.0", "0.19.5") > 0);
        assertEquals(0, TargetGuard.compareVersions("0.19.5", "0.19.5"));
        assertEquals(0, TargetGuard.compareVersions("1.0", "1.0.0"));
        assertTrue(TargetGuard.compareVersions("0.9.0", "0.10.0") < 0);
        assertTrue(TargetGuard.compareVersions("2.0.0", "10.0.0") < 0);
    }

    @Test
    void jsonCarriesEverythingAReaderNeeds() {
        TargetGuard.Decision decision =
                TargetGuard.evaluate(Optional.of(fabric262()), matching(), TargetGuard.Policy.ENFORCE);

        String json = TargetGuard.toJson(fabric262(), matching(), decision);

        assertTrue(json.contains("\"target\":\"fabric-26.2\""), json);
        assertTrue(json.contains("\"result\":\"ok\""), json);
        assertTrue(json.contains("\"policy\":\"enforce\""), json);
        assertTrue(json.contains("\"gameVersion\":\"26.2\""), json);
        assertTrue(json.contains("\"loaderVersion\":\"0.19.5\""), json);
        assertTrue(json.contains("\"java\":25"), json);
        assertTrue(json.contains("\"reasons\":[]"), json);
    }
}
