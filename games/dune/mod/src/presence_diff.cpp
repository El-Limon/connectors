#include "presence_diff.h"

#include <algorithm>

namespace {

PresenceDiff::Tracked* Find(std::vector<PresenceDiff::Tracked>& tracked, const void* c) {
    for (auto& t : tracked)
        if (t.controller == c) return &t;
    return nullptr;
}

bool Contains(const std::vector<const void*>& v, const void* c) {
    return std::find(v.begin(), v.end(), c) != v.end();
}

}  // namespace

PresenceDiff::Result PresenceDiff::Apply(std::vector<Tracked>& tracked, const std::vector<const void*>& connectedNow,
                                        const Options& opt,
                                        const std::function<bool(const void*)>& preAnnounced) {
    Result r;

    // 1. Everything that is connected right now: reset its miss counter, adopt it if it is new.
    for (const void* c : connectedNow) {
        if (!c) continue;
        Tracked* t = Find(tracked, c);
        if (!t) {
            if (tracked.size() >= opt.maxTracked) continue;  // bounded, like the registry itself
            Tracked nt;
            nt.controller = c;
            // If something else already holds this controller online (the PostLogin detour got there
            // first), adopt it as announced so the sweep never emits a second `player-connected`.
            nt.announced = preAnnounced ? preAnnounced(c) : false;
            tracked.push_back(nt);
            t = &tracked.back();
        }
        t->misses = 0;
        if (!t->announced) r.connects.push_back(c);
    }

    // 2. Everything tracked that is NOT connected right now: debounce, then confirm.
    for (size_t i = 0; i < tracked.size();) {
        Tracked& t = tracked[i];
        if (Contains(connectedNow, t.controller)) {
            i++;
            continue;
        }
        t.misses++;
        if (t.misses < opt.missesBeforeDisconnect) {
            i++;
            continue;
        }
        // Confirmed gone. Only announce a disconnect for a player we announced AND whom nothing else
        // has already retired — `preAnnounced` false here means the Logout detour already emitted.
        const void* c = t.controller;
        bool stillOnlineElsewhere = preAnnounced ? preAnnounced(c) : false;
        if (t.announced && stillOnlineElsewhere) {
            r.disconnects.push_back(c);
        } else {
            r.droppedUnannounced++;
        }
        tracked.erase(tracked.begin() + (long)i);
    }
    return r;
}

void PresenceDiff::MarkAnnounced(std::vector<Tracked>& tracked, const void* controller) {
    Tracked* t = Find(tracked, controller);
    if (t) t->announced = true;
}
