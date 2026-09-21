package io.takaro.minecraft.core.target;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;

/**
 * Decides whether this jar belongs on this server.
 *
 * A connector built for one game version silently misbehaves on another: actions fail,
 * events stop arriving, and the failure looks like a Takaro problem. The guard turns that
 * into one refusal with a reason, before the WebSocket is ever opened.
 */
public final class TargetGuard {

    public enum Policy {
        ENFORCE, WARN, OFF;

        public static Policy parse(String raw) {
            if (raw == null) {
                return ENFORCE;
            }
            return switch (raw.trim().toLowerCase()) {
                case "warn" -> WARN;
                case "off", "false", "0", "none" -> OFF;
                default -> ENFORCE;
            };
        }
    }

    /** ok, refuse or unchecked, plus whether the connector may still connect. */
    public record Decision(String result, Policy policy, boolean connect, List<String> reasons) {
    }

    private static final Gson GSON = new Gson();

    private TargetGuard() {
    }

    public static Decision evaluate(Optional<TargetInfo> info, RuntimeIdentity identity, Policy policy) {
        if (policy == Policy.OFF) {
            return new Decision("unchecked", policy, true, List.of("target checking is disabled"));
        }
        if (info == null || info.isEmpty()) {
            return new Decision("unchecked", policy, true,
                    List.of("this build carries no " + TargetInfo.RESOURCE + " stamp"));
        }
        if (identity == null) {
            return new Decision("unchecked", policy, true,
                    List.of("this platform does not report a runtime identity"));
        }

        TargetInfo target = info.get();
        List<String> reasons = new ArrayList<>();
        String expectedGameVersion = target.gameVersion().isEmpty() ? target.revision() : target.gameVersion();
        if (!expectedGameVersion.isEmpty() && !expectedGameVersion.equals(identity.gameVersion())) {
            reasons.add("built for game version " + expectedGameVersion + ", this server runs "
                    + identity.gameVersion());
        }
        String loaderVersion = identity.loaderVersion();
        boolean loaderKnown = loaderVersion != null && !loaderVersion.isBlank();
        if (!target.loaderMin().isEmpty() && loaderKnown
                && compareVersions(loaderVersion, target.loaderMin()) < 0) {
            reasons.add("needs loader " + target.loaderMin() + " or newer, this server runs "
                    + loaderVersion);
        }
        if (target.javaMin() > 0 && identity.javaMajor() > 0 && identity.javaMajor() < target.javaMin()) {
            reasons.add("needs Java " + target.javaMin() + " or newer, this server runs Java "
                    + identity.javaMajor());
        }

        if (reasons.isEmpty()) {
            return new Decision("ok", policy, true, List.of());
        }
        return new Decision("refuse", policy, policy == Policy.WARN, List.copyOf(reasons));
    }

    /**
     * Dotted version compare: numeric segments numerically, anything else as text,
     * missing segments as 0. "0.19.3" &lt; "0.19.5" &lt; "0.20.0".
     */
    public static int compareVersions(String left, String right) {
        String[] leftParts = split(left);
        String[] rightParts = split(right);
        int length = Math.max(leftParts.length, rightParts.length);
        for (int i = 0; i < length; i++) {
            String a = i < leftParts.length ? leftParts[i] : "0";
            String b = i < rightParts.length ? rightParts[i] : "0";
            int comparison;
            if (isNumeric(a) && isNumeric(b)) {
                comparison = Long.compare(Long.parseLong(a), Long.parseLong(b));
            } else {
                comparison = a.compareTo(b);
            }
            if (comparison != 0) {
                return comparison < 0 ? -1 : 1;
            }
        }
        return 0;
    }

    private static String[] split(String version) {
        if (version == null || version.isEmpty()) {
            return new String[] {"0"};
        }
        return version.split("[.+-]");
    }

    private static boolean isNumeric(String text) {
        if (text.isEmpty()) {
            return false;
        }
        for (int i = 0; i < text.length(); i++) {
            if (!Character.isDigit(text.charAt(i))) {
                return false;
            }
        }
        return true;
    }

    /** The one line the harness and a human both read to see what the guard decided. */
    public static String toJson(TargetInfo info, RuntimeIdentity identity, Decision decision) {
        JsonObject root = new JsonObject();
        root.addProperty("target", info == null ? null : info.target());
        root.addProperty("fingerprint", info == null ? null : info.fingerprint());
        root.addProperty("connectorVersion", info == null ? null : info.connectorVersion());
        JsonObject runtime = new JsonObject();
        runtime.addProperty("gameVersion", identity == null ? null : identity.gameVersion());
        runtime.addProperty("loader", identity == null ? null : identity.loader());
        runtime.addProperty("loaderVersion", identity == null ? null : identity.loaderVersion());
        if (identity != null) {
            runtime.addProperty("java", identity.javaMajor());
        } else {
            runtime.add("java", GSON.toJsonTree(null));
        }
        root.add("runtime", runtime);
        root.addProperty("policy", decision.policy().name().toLowerCase());
        root.addProperty("result", decision.result());
        JsonArray reasons = new JsonArray();
        decision.reasons().forEach(reasons::add);
        root.add("reasons", reasons);
        return GSON.toJson(root);
    }
}
