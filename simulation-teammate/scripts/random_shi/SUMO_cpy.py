import threading
import queue
import time
import traci

class SUMO:
    def __init__(self):
        self.command_queue = queue.Queue()
        self.sumo_thread = None
        self.car_id = 0

        self.routes = {
            "route_WE" : ["1167298926#0", "492431226#0"],
            "route_EW" :["23415702#2", "492431223#0"],
            "route_NS" :["1167293941#1", "27227507#0"],
            "route_SN" :["543470383#4", "464465177#0"]
        }

    def background_thread(self):
        traci.start(["sumo-gui", "-c", "osm.sumocfg"])

        for rt in self.routes:
            traci.route.add(rt, self.routes[rt])

        while True:
            # Process any pending commands sent from the main thread
            while not self.command_queue.empty():
                cmd_type, payload = self.command_queue.get()

                if cmd_type == "SPAWN":
                    veh_id, route_id = payload
                    traci.vehicle.add(f"car_{veh_id}", route_id, typeID="DEFAULT_VEHTYPE")
                    self.car_id += 1

                else:
                    print(">>> Invalid Command ...")

            # Step the simulation
            traci.simulationStep()
            time.sleep(0.01)  # Optional delay to control simulation speed

    def start_sim(self):
        self.sumo_thread = threading.Thread(
            target = self.background_thread
        )

        self.sumo_thread.start()

    def add_car(self):
        self.command_queue.put(("SPAWN", ("1", "route_WE")))
        self.command_queue.put(("SPAWN", ("2", "route_WE")))
        self.command_queue.put(("SPAWN", ("3", "route_WE")))
        self.command_queue.put(("SPAWN", ("4", "route_WE")))
        self.command_queue.put(("SPAWN", ("5", "route_WE")))
