package io.takaro.zomboid.agent;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * The runtime target guard's decision table. The decision is a pure function of what was
 * observed, so every branch is exercised here without a Project Zomboid server.
 */
class TargetGuardTest {

    private static final String PINNED = "80e405a4bfc42f6072e75b3735f458a6514143da011d3226007ded305a442f44";
    private static final String OTHER = "1111111111111111111111111111111111111111111111111111111111111111";

    @Test
    void theRightServerBuildIsAccepted() {
        TargetGuard.Decision decision = TargetGuard.decide("enforce", PINNED, PINNED, true, true);

        assertEquals("ok", decision.result());
        assertTrue(decision.proceed());
        assertTrue(decision.reasons().isEmpty());
    }

    @Test
    void anotherServerBuildIsRefusedUnderEnforce() {
        TargetGuard.Decision decision = TargetGuard.decide("enforce", PINNED, OTHER, true, true);

        assertEquals("refuse", decision.result());
        assertFalse(decision.proceed());
        assertTrue(decision.reasons().stream().anyMatch(r -> r.contains(OTHER) && r.contains(PINNED)),
                "the reason names both hashes: " + decision.reasons());
    }

    @Test
    void warnContinuesAfterSayingSo() {
        TargetGuard.Decision decision = TargetGuard.decide("warn", PINNED, OTHER, true, true);

        assertEquals("refuse", decision.result());
        assertTrue(decision.proceed());
        assertTrue(decision.reasons().stream().anyMatch(r -> r.contains("warn")));
    }

    @Test
    void anUnknownPolicyIsTreatedAsEnforce() {
        assertFalse(TargetGuard.decide("nonsense", PINNED, OTHER, true, true).proceed());
        assertFalse(TargetGuard.decide(null, PINNED, OTHER, true, true).proceed());
    }

    @Test
    void offDoesNotCheckAtAll() {
        TargetGuard.Decision decision = TargetGuard.decide("off", PINNED, null, true, false);

        assertEquals("skipped", decision.result());
        assertTrue(decision.proceed());
    }

    @Test
    void aJarWithoutATargetIsUnpinnedNotRefused() {
        TargetGuard.Decision decision = TargetGuard.decide("enforce", null, PINNED, false, true);

        assertEquals("unpinned", decision.result());
        assertTrue(decision.proceed());
    }

    @Test
    void aTargetWithoutAPinnedHashIsUnpinned() {
        TargetGuard.Decision decision = TargetGuard.decide("enforce", "", PINNED, true, true);

        assertEquals("unpinned", decision.result());
        assertTrue(decision.proceed());
    }

    @Test
    void aProbeJvmWithoutTheGameJarContinues() {
        // premain runs three times per boot; the first two JVMs never load the game.
        TargetGuard.Decision decision = TargetGuard.decide("enforce", PINNED, null, true, false);

        assertEquals("no-game-jar", decision.result());
        assertTrue(decision.proceed());
    }

    @Test
    void theHashIsTheOneSha256sumWouldPrint(@TempDir Path dir) throws Exception {
        Path file = dir.resolve("payload.bin");
        Files.write(file, "takaro".getBytes(StandardCharsets.UTF_8));

        // printf takaro | sha256sum
        assertEquals("1c3b1c0cedbb2ddf5f4d03a36ab1b111370e4043372b8f43eecb1bddfb6057ce", TargetGuard.sha256(file));
    }
}
