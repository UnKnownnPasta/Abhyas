import pygame
import os
import sys
import traci
import threading
import queue
import time

# Initialize Pygame
pygame.init()

# Window Setup
WIDTH, HEIGHT = 400, 200
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("Pygame Button Template")
clock = pygame.time.Clock()

# Colors
BG_COLOR = (240, 240, 240)
BTN_COLOR = (70, 130, 180)
BTN_HOVER_COLOR = (100, 160, 210)
TEXT_COLOR = (255, 255, 255)

# Button Setup
font = pygame.font.Font(None, 28)
button_rect = pygame.Rect(125, 75, 150, 50)
button_text = font.render("Put car", True, TEXT_COLOR)
text_rect = button_text.get_rect(center=button_rect.center)

# Queue for passing commands (e.g., spawn car, move vehicle) from Main -> TraCI thread
command_queue = queue.Queue()

def sumo_worker():
    """Background thread running SUMO continuously."""

    traci.start(["sumo-gui", "-c", "osm.sumocfg"])
    car_id = 1 

    traci.route.add(
        "route_WE", 
        [
            "1167298926#0",
            "492431226#0"
        ]
    )
    
    while True:
        # Process any pending commands sent from the main thread
        while not command_queue.empty():
            cmd_type, payload = command_queue.get()

            if cmd_type == "SPAWN":
                veh_id, route_id = payload
                traci.vehicle.add(f"car_{car_id}", "route_WE", typeID="DEFAULT_VEHTYPE")
                car_id += 1

            else:
                print(">>> Invalid Command ...")

        # Step the simulation
        traci.simulationStep()
        time.sleep(0.01)  # Optional delay to control simulation speed

def add_car():
    command_queue.put(("SPAWN", ("car_1", "route_WE")))

# start sumo as background thread
sumo_thread = threading.Thread(target=sumo_worker, daemon=True)
sumo_thread.start()

# Main Loop
running = True
while running:
    mouse_pos = pygame.mouse.get_pos()
    
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
            
        elif event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 1 and button_rect.collidepoint(mouse_pos):
                add_car()

    # Drawing
    screen.fill(BG_COLOR)
    
    # Hover effect
    is_hovered = button_rect.collidepoint(mouse_pos)
    current_color = BTN_HOVER_COLOR if is_hovered else BTN_COLOR
    
    pygame.draw.rect(screen, current_color, button_rect, border_radius=6)
    screen.blit(button_text, text_rect)
    
    pygame.display.flip()
    clock.tick(60)

pygame.quit()
sys.exit()