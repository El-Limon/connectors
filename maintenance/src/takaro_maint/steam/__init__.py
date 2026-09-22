"""Exact acquisition of a Steam-delivered dedicated server.

Steam publishes no download link and no stable archive: a build is an app on a branch
plus one content manifest per depot, and the branch head moves whenever the publisher
pushes. Pinning the manifest ids is what makes a Steam server reproducible, and this
package is the only place that talks to Steam.
"""

from __future__ import annotations

from . import depotdownloader, install

__all__ = ["depotdownloader", "install"]
