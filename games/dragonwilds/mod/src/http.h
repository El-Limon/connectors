// Loopback HTTP/1.1 server (127.0.0.1:18890 by default), bearer-token authenticated.
#pragma once
#include "common.h"

namespace Http {
void Start();          // spawns the listener thread; never blocks
int Port();
bool TokenConfigured();
std::string StatsJson();
}  // namespace Http
