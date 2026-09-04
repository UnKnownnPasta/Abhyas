import math
import os
import queue
import sys
import threading
import time
import traci

# Ensure script directory is in path for RenderingBackend import
_script_dir = os.path.dirname(os.path.abspath(__file__))
if _script_dir not in sys.path:
    sys.path.append(_script_dir)

try:
    from RenderingBackend import RenderingBackend
except ImportError:
    from scripts.RenderingBackend import RenderingBackend


class SUMO:
    """SUMO simulation manager with TraCI background thread and 3D rendering backend integration."""

    def __init__(self, config_file: str = "osm.sumocfg", gui: bool = True):
        self.config_file = config_file
        self.gui = gui
        self.command_queue = queue.Queue()
        self.sumo_thread = None
        self.car_id = 0

        self._lock = threading.Lock()
        self._vehicles: list[dict] = []
        self._signals: list[dict] = []
        self.running = False
        self.spawned_vehicles: list[tuple[str, str]] = []

        self.routes = {
            "route_WE": ["1167298926#0", "492431226#0"],
            "route_EW": ["23415702#2", "492431223#0"],
            "route_NS": ["1167293941#1", "27227507#0"],
            "route_SN": ["543470383#4", "464465177#0"],
        }

    def background_thread(self):
        sumo_binary = "sumo-gui" if self.gui else "sumo"
        try:
            traci.start([sumo_binary, "-c", self.config_file])
        except Exception as e:
            print(f"Error starting TraCI/SUMO: {e}")
            return

        for rt in self.routes:
            try:
                traci.route.add(rt, self.routes[rt])
            except Exception:
                pass

        self.running = True

        try:
            while self.running:
                # Process any pending commands sent from the main thread
                while not self.command_queue.empty():
                    cmd_type, payload = self.command_queue.get()

                    if cmd_type == "SPAWN":
                        veh_id, route_id = payload
                        try:
                            traci.vehicle.add(f"car_{veh_id}", route_id, typeID="DEFAULT_VEHTYPE")
                            self.spawned_vehicles.append((str(veh_id), route_id))
                        except Exception as e:
                            print(f"Error adding vehicle car_{veh_id}: {e}")
                    elif cmd_type == "ADD_ROUTE":
                        route_id, edges = payload
                        try:
                            traci.route.add(route_id, edges)
                        except Exception as e:
                            print(f"Error adding route {route_id}: {e}")
                    elif cmd_type == "RESET":
                        try:
                            traci.load(["-c", self.config_file])
                            for rt in self.routes:
                                try:
                                    traci.route.add(rt, self.routes[rt])
                                except Exception:
                                    pass
                            for veh_id, route_id in self.spawned_vehicles:
                                try:
                                    traci.vehicle.add(f"car_{veh_id}", route_id, typeID="DEFAULT_VEHTYPE")
                                except Exception:
                                    pass
                            with self._lock:
                                self._vehicles.clear()
                                self._signals.clear()
                            print(">>> SUMO Simulation Reset: All vehicles returned to initial positions on routes.")
                        except Exception as e:
                            print(f">>> Error resetting SUMO simulation: {e}")
                    else:
                        print(">>> Invalid Command ...")

                # Step the simulation
                traci.simulationStep()

                # Fetch active vehicle states
                veh_ids = traci.vehicle.getIDList()
                current_vehicles = []
                for v_id in veh_ids:
                    try:
                        x, y = traci.vehicle.getPosition(v_id)
                        angle = traci.vehicle.getAngle(v_id)
                        length = traci.vehicle.getLength(v_id)
                        width = traci.vehicle.getWidth(v_id)
                        height = traci.vehicle.getHeight(v_id)
                        color = traci.vehicle.getColor(v_id)
                        speed = traci.vehicle.getSpeed(v_id)
                        current_vehicles.append(
                            {
                                "id": v_id,
                                "x": x,
                                "y": y,
                                "angle": angle,
                                "length": length,
                                "width": width,
                                "height": height,
                                "color": color[:3],
                                "speed": speed,
                            }
                        )
                    except Exception:
                        pass

                # Fetch traffic light signal states
                tl_ids = traci.trafficlight.getIDList()
                current_signals = []
                for tl_id in tl_ids:
                    try:
                        state = traci.trafficlight.getRedYellowGreenState(tl_id)
                        links = traci.trafficlight.getControlledLinks(tl_id)
                        n = min(len(state), len(links))
                        for i in range(n):
                            if links[i]:
                                inc_lane = links[i][0][0]
                                shape = traci.lane.getShape(inc_lane)
                                if len(shape) >= 1:
                                    stop_x, stop_y = shape[-1]
                                    heading = 0.0
                                    if len(shape) >= 2:
                                        x1, y1 = shape[-2]
                                        x2, y2 = shape[-1]
                                        heading = math.degrees(math.atan2(x2 - x1, y2 - y1))
                                    current_signals.append(
                                        {
                                            "id": f"{tl_id}_{i}",
                                            "x": stop_x,
                                            "y": stop_y,
                                            "state": state[i],
                                            "heading_deg": heading,
                                        }
                                    )
                    except Exception:
                        pass

                with self._lock:
                    self._vehicles = current_vehicles
                    self._signals = current_signals

                time.sleep(0.01)
        finally:
            self.running = False
            try:
                traci.close()
            except Exception:
                pass

    def start_sim(self):
        self.sumo_thread = threading.Thread(target=self.background_thread, daemon=True)
        self.sumo_thread.start()

    def stop_sim(self):
        self.running = False
        if self.sumo_thread and self.sumo_thread.is_alive():
            self.sumo_thread.join(timeout=2.0)

    def get_vehicles(self) -> list[dict]:
        with self._lock:
            return list(self._vehicles)

    def get_signals(self) -> list[dict]:
        with self._lock:
            return list(self._signals)

    def reset_sim(self):
        """Sends a command to reset the TraCI simulation."""
        self.command_queue.put(("RESET", None))

    def add_route(self, route_id: str, edges: list[str]):
        """Adds a route to the simulation and stores it in self.routes so it survives resets."""
        self.routes[route_id] = edges
        self.command_queue.put(("ADD_ROUTE", (route_id, edges)))

    def add_car(self, route_id: str = "route_WE"):
        self.car_id += 1
        self.command_queue.put(("SPAWN", (str(self.car_id), route_id)))

    def render_vehicles(
        self,
        net_path: str = "osm.net.xml",
        renderer: RenderingBackend | None = None,
        width: int = 1280,
        height: int = 720,
    ):
        """Reads all active vehicles and signals from SUMO and renders them using RenderingBackend."""
        import pyray as rl

        close_when_done = False
        if renderer is None:
            renderer = RenderingBackend(net_path=net_path, width=width, height=height, title="SUMO 3D Simulation")
            close_when_done = True

        renderer.open()
        try:
            while renderer.begin_frame():
                if rl.is_key_pressed(rl.KEY_R):
                    self.reset_sim()

                vehs = self.get_vehicles()
                sigs = self.get_signals()

                renderer.hud_extra = f"Vehicles: {len(vehs)}  |  [R] Reset"
                renderer.draw_vehicles(vehs)
                renderer.draw_signals(sigs)

                renderer.end_frame()
        finally:
            if close_when_done:
                renderer.close()
