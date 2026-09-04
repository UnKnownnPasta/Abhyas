import os
import sys
import time

_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.append(_script_dir)

import SUMO

if __name__ == "__main__":
    sim = SUMO.SUMO()
    sim.start_sim()

    # Spawn test vehicles on different routes
    time.sleep(1.0)
    sim.add_car("route_WE")
    sim.add_car("route_EW")
    sim.add_car("route_NS")
    sim.add_car("route_SN")

    # Render live simulation with 3D RenderingBackend
    sim.render_vehicles(net_path="osm.net.xml")