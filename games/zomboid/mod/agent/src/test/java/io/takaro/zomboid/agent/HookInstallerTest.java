package io.takaro.zomboid.agent;

import static net.bytebuddy.matcher.ElementMatchers.isStatic;
import static net.bytebuddy.matcher.ElementMatchers.named;
import static net.bytebuddy.matcher.ElementMatchers.takesArgument;
import static net.bytebuddy.matcher.ElementMatchers.takesArguments;
import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import java.util.Map;

import net.bytebuddy.description.method.MethodDescription;
import net.bytebuddy.description.type.TypeDescription;
import net.bytebuddy.matcher.ElementMatcher;

import org.junit.jupiter.api.Test;

class HookInstallerTest {

    static final class HasTick {
        static void update() {
        }
    }

    static final class HasNoTick {
        static void anotherMethod() {
        }
    }

    static final class ChatMessage {
    }

    static final class HasChat {
        void sendMessage(ChatMessage message) {
        }
    }

    static final class HasWrongChat {
        void sendMessage(String message) {
        }
    }

    @Test
    void reportsWhetherTheTickMatcherBinds() {
        ElementMatcher.Junction<MethodDescription> tick =
                named("update").and(isStatic()).and(takesArguments(0));

        assertEquals(List.of(), unbound(HasTick.class, "tick", tick));
        assertEquals(List.of("tick"), unbound(HasNoTick.class, "tick", tick));
    }

    @Test
    void reportsWhetherTheChatMessageShapeBinds() {
        ElementMatcher.Junction<MethodDescription> chat = named("sendMessage")
                .and(takesArgument(0, named(ChatMessage.class.getName())));

        assertEquals(List.of(), unbound(HasChat.class, "chat", chat));
        assertEquals(List.of("chat"), unbound(HasWrongChat.class, "chat", chat));
    }

    private static List<String> unbound(
            Class<?> type,
            String name,
            ElementMatcher<? super MethodDescription> matcher) {
        return HookInstaller.unbound(
                new TypeDescription.ForLoadedType(type),
                Map.of(name, matcher));
    }
}
