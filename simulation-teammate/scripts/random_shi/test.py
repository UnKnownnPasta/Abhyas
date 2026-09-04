import os
import sys
import traci
import time


# Ensure SUMO tools are in Python path
if 'SUMO_HOME' in os.environ:
    sys.path.append(os.path.join(os.environ['SUMO_HOME'], 'tools'))

# 1. Start SUMO GUI or CLI
sumo_cmd = ["sumo-gui", "-c", "osm.sumocfg"]
traci.start(sumo_cmd)

step = 0
while True:
    traci.simulationStep()
    
    # 2. Add a route dynamically at step 10
    if step == 10:
        # Define route_ID and list of edge IDs matching your .net.xml
        traci.route.add(
            "route_NS", 
            [
                "1167298926#0",
                "-36054718#1"
            ]
        )
        
        # 3. Spawn a vehicle on that route
        # Parameters: (vehID, routeID, typeID)
        traci.vehicle.add("car_1", "route_NS", typeID="DEFAULT_VEHTYPE")
        
        # Optional: Set properties on the spawned car dynamically
        traci.vehicle.setSpeed("car_1", 12.5) # Set speed in m/s (~45 km/h)
        traci.vehicle.setColor("car_1", (255, 0, 0, 255)) # Paint it Red (R, G, B, A)

    step += 1

    time.sleep(1/60)

traci.close()