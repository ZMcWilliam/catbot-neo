import time
import cv2
import json
import math
import helper_camera
import helper_motorkit as m
import helper_intersections
import numpy as np
import threading
from gpiozero import AngularServo
from typing import List, Tuple

# Type aliases
Contour = List[List[Tuple[int, int]]]

PORT_SERVO_GATE = 12
PORT_SERVO_CLAW = 13
PORT_SERVO_LIFT = 18
PORT_SERVO_CAM = 19

servo = {
    "gate": AngularServo(PORT_SERVO_GATE, min_pulse_width=0.0006, max_pulse_width=0.002, initial_angle=-90),    # -90=Close, 90=Open
    "claw": AngularServo(PORT_SERVO_CLAW, min_pulse_width=0.0005, max_pulse_width=0.002, initial_angle=-80),    # 0=Open, -90=Close
    "lift": AngularServo(PORT_SERVO_LIFT, min_pulse_width=0.0005, max_pulse_width=0.0025, initial_angle=-80),   # -90=Up, 40=Down
    "cam": AngularServo(PORT_SERVO_CAM, min_pulse_width=0.0006, max_pulse_width=0.002, initial_angle=-83)       # -90=Down, 90=Up
}

cams = helper_camera.CameraController()
cams.start_stream(0)

#System variables
changed_angle = False
last_line_pos = np.array([100,100])
last_ang = 0
current_linefollowing_state = None
white_intersection_cooldown = 0
changed_black_contour = False

intersection_state_debug = ["", time.time()]

# max_error_and_angle = 285 + 90
max_error = 285
max_angle = 90
error_weight = 0.5
angle_weight = 1-error_weight

#Configs

# Load the calibration map from the JSON file
with open("calibration.json", "r") as json_file:
    calibration_data = json.load(json_file)
calibration_map = np.array(calibration_data["calibration_map_w"])

with open("config.json", "r") as json_file:
    config_data = json.load(json_file)

black_contour_threshold = 5000
config_values = {
    "black_line_threshold": config_data["black_line_threshold"],
    "green_turn_hsv_threshold": [np.array(bound) for bound in config_data["green_turn_hsv_threshold"]]
}

# Constants for PID control
KP = 1 # Proportional gain
KI = 0  # Integral gain
KD = 0.1  # Derivative gain
follower_speed = 40

lastError = 0
integral = 0
# Motor stuff
max_motor_speed = 100

greenCenter = None


# Jank functions

# Calculate the distance between a point, and the last line position
def distToLastLine(point):
    if (point[0][0] > last_line_pos[0]):
        return np.linalg.norm(np.array(point[0]) - last_line_pos)
    else:
        return np.linalg.norm(last_line_pos - point[0])
    
# Vectorize the distance function so it can be applied to a numpy array
# This helps speed up calculations when calculating the distance of many points
distToLastLineFormula = np.vectorize(distToLastLine)

# Processes a set of contours to find the best one to follow
# Filters out contours that are too small, 
# then, sorts the remaining contours by distance from the last line position
def FindBestContours(contours):
    """
    Processes a set of contours to find the best one to follow
    Filters out contours that are too small,
    then, sorts the remaining contours by distance from the last line position
    
    Returns:
        contour_values: A numpy array of contours, sorted by distance from the last line position
        [
            contour_area: float,
            contour_rect: cv2.minAreaRect,
            contour: np.array,
            distance_from_last_line: float
        ]
    """
    # Create a new array with the contour area, contour, and distance from the last line position (to be calculated later)
    contour_values = np.array([[cv2.contourArea(contour), cv2.minAreaRect(contour), contour, 0] for contour in contours ], dtype=object)

    # In case we have no contours, just return an empty array instead of processing any more
    if len(contour_values) == 0:
        return []
    
    # Filter out contours that are too small
    contour_values = contour_values[contour_values[:, 0] > black_contour_threshold]
    
    # No need to sort if there is only one contour
    if len(contour_values) <= 1:
        return contour_values

    # Sort contours by distance from the last known optimal line position
    contour_values[:, 3] = distToLastLineFormula(contour_values[:, 1])
    contour_values = contour_values[np.argsort(contour_values[:, 3])]
    return contour_values

current_time = time.time()

def pid(error): # Calculate error beforehand
    global current_time, integral, lastError
    timeDiff = time.time() - current_time
    if (timeDiff == 0):
        timeDiff = 1/10
    proportional = KP*(error)
    integral += KI*error*timeDiff
    derivative = KD*(error-lastError)/timeDiff
    PIDOutput = -(proportional + integral + derivative)
    lastError = error
    current_time = time.time()
    return PIDOutput

def centerOfContour(contour):
    M = cv2.moments(contour)
    cX = int(M["m10"] / M["m00"])
    cY = int(M["m01"] / M["m00"])
    return (cX, cY)
    # x, y, w, h = cv2.boundingRect(contour)
    # return (int(x+(w/2)), int(y+(h/2)))
def centerOfLine(line):
    return (int((line[0][0]+line[1][0])/2), int((line[0][1]+line[1][1])/2))

def pointDistance(p1, p2):
    return math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2)
def midpoint(p1, p2):
    return ((p1[0]+p2[0])/2, (p1[1]+p2[1])/2)

frames = 0
# check_thing = False
start_time = time.time()
# last_green_found_time = start_time - 1000
last_intersection_time = time.time() - 100
fpsTime = time.time()
delay = time.time()

double_check = 0
gzDetected = False

# Simplifies a given contour by reducing the number of points while maintaining the general shape
# epsilon controls the level of simplification, with higher values resulting in more simplification
# Then, returns the simplified contour as a list of points
def simplifiedContourPoints(contour, epsilon=0.01):
    epsilonBL = epsilon * cv2.arcLength(contour, True)
    return [pt[0] for pt in cv2.approxPolyDP(contour, epsilonBL, True)]

smallKernel = np.ones((5,5),np.uint8)

# MAIN LOOP
while True:
    if frames % 20 == 0 and frames != 0:
        print(f"Processing FPS: {20/(time.time()-fpsTime)}")
        fpsTime = time.time()
    # if frames % 100 == 0:
    #     print(f"Camera 0 average FPS: {cams.get_fps(0)}")
    img0 = cams.read_stream(0)
    # cv2.imwrite("testImg.jpg", img0)
    if (img0 is None):
        continue
    img0 = img0.copy()

    img0 = img0[0:img0.shape[0]-38, 0:img0.shape[1]-70]
    img0 = cv2.cvtColor(img0, cv2.COLOR_BGR2RGB)

    img0_clean = img0.copy() # Used for displaying the image without any overlays

    # #Find the black in the image
    img0_gray = cv2.cvtColor(img0, cv2.COLOR_RGB2GRAY)
    # img0_gray = cv2.equalizeHist(img0_gray)
    img0_gray = cv2.GaussianBlur(img0_gray, (5, 5), 0)

    img0_gray_scaled = 255 / np.clip(calibration_map, a_min=1, a_max=None) * img0_gray  # Scale white values based on the inverse of the calibration map
    img0_gray_scaled = np.clip(img0_gray_scaled, 0, 255)    # Clip the scaled image to ensure values are within the valid range
    img0_gray_scaled = img0_gray_scaled.astype(np.uint8)    # Convert the scaled image back to uint8 data type

    img0_binary = cv2.threshold(img0_gray_scaled, config_values["black_line_threshold"][0], config_values["black_line_threshold"][1], cv2.THRESH_BINARY)[1]
    img0_binary = cv2.morphologyEx(img0_binary, cv2.MORPH_OPEN, np.ones((7,7),np.uint8))

    img0_hsv = cv2.cvtColor(img0, cv2.COLOR_RGB2HSV)

    #Find the green in the image
    img0_green = cv2.bitwise_not(cv2.inRange(img0_hsv, config_values["green_turn_hsv_threshold"][0], config_values["green_turn_hsv_threshold"][1]))
    img0_green = cv2.erode(img0_green, np.ones((5,5),np.uint8), iterations=1)

    # #Remove the green from the black (since green looks like black when grayscaled)
    img0_line = cv2.dilate(img0_binary, np.ones((5,5),np.uint8), iterations=2)
    img0_line = cv2.bitwise_or(img0_binary, cv2.bitwise_not(img0_green))

    # -----------

    raw_white_contours, white_hierarchy = cv2.findContours(img0_line, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    # Filter white contours based on area
    white_contours = []
    for contour in raw_white_contours:
        if (cv2.contourArea(contour) > 1000):
            white_contours.append(contour)

    if (len(white_contours) == 0):
        print("No white contours found")
        continue

    
    # Find black contours
    # If there are no black contours, skip the rest of the loop
    img0_line_not = cv2.bitwise_not(img0_line)
    black_contours, black_hierarchy = cv2.findContours(img0_line_not, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    if (len(black_contours) == 0):
        print("No black contours found")
        continue
    
    # -----------
    # GREEN TURNS
    # -----------

    is_there_green = np.count_nonzero(img0_green == 0)
    turning = False
    black_contours_turn = None

    # print("Green: ", is_there_green)
    
    # Check if there is a significant amount of green pixels
    if is_there_green > 2000: #and len(white_contours) > 2: #((is_there_green > 1000 or time.time() - last_green_found_time < 0.5) and (len(white_contours) > 2 or greenCenter is not None)):
        unfiltered_green_contours, green_hierarchy = cv2.findContours(cv2.bitwise_not(img0_green), cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        # cv2.drawContours(img0, green_contours[0], -1, (255,255,0), 3)

        # TODO

        print("GREEN TURN STUFF")
    else:
        greenCenter = None # Reset greenturn memory if no green found!


    # -----------
    # INTERSECTIONS
    # -----------
    if not turning:
        img0_line_new = img0_line.copy()

        # Filter white contours to have a minimum area before we accept them 
        white_contours_filtered = [contour for contour in white_contours if cv2.contourArea(contour) > 500]

        if len(white_contours_filtered) == 2:
            # Sort contours based on x position
            contour_L = white_contours_filtered[0]
            contour_R = white_contours_filtered[1]

            if centerOfContour(contour_L) > centerOfContour(contour_R):
                contour_L = white_contours_filtered[1]
                contour_R = white_contours_filtered[0]

            # Simplify contours to get key points
            contour_L_simple = simplifiedContourPoints(contour_L, 0.03)
            contour_R_simple = simplifiedContourPoints(contour_R, 0.03)

            # Sort the simplified contours based on their coordinates
            left_cutter_points = sorted(sorted(contour_R_simple, key=lambda point: point[0])[:2], key=lambda point: point[1])
            right_cutter_points = sorted(sorted(contour_L_simple, key=lambda point: -point[0])[:2], key=lambda point: point[1])
            
            if current_linefollowing_state is None:
                # Only one of the cutter points is at the top of the screen
                # This means we have just started seeing a line coming off to the side
                if (
                    (left_cutter_points[0][1] > 3 and right_cutter_points[0][1] < 3 or left_cutter_points[0][1] < 3 and right_cutter_points[0][1] > 3)
                    and not ((centerOfLine(left_cutter_points)[0] < centerOfLine(right_cutter_points)[0]))
                ):
                    if (white_intersection_cooldown == 0):
                        white_intersection_cooldown = time.time()
                    if (time.time()-white_intersection_cooldown < 2):
                        img0_line_new = helper_intersections.CutMaskWithLine(left_cutter_points[1], left_cutter_points[0], img0_line_new, "right")
                        img0_line_new = cv2.bitwise_not(helper_intersections.CutMaskWithLine(right_cutter_points[1], right_cutter_points[0], img0_line_new, "left"))
                        current_linefollowing_state = "2-ng-a"
                        changed_black_contour = img0_line_new
                        intersection_state_debug = ["2-ng-a", time.time()]
                    else:
                        current_linefollowing_state = None
                        intersection_state_debug = ["2-ng-b", time.time()]
                # Both cutter points are not at the top of the screen
                elif (left_cutter_points[0][1] > 3 and right_cutter_points[0][1] > 3):
                    img0_line_new = helper_intersections.CutMaskWithLine(left_cutter_points[1], left_cutter_points[0], img0_line_new, "right")
                    img0_line_new = cv2.bitwise_not(helper_intersections.CutMaskWithLine(right_cutter_points[0], right_cutter_points[1], img0_line_new, "left"))
                    # img0_line_new = cv2.bitwise_not(img0_line_new)
                    changed_black_contour = img0_line_new
                    current_linefollowing_state = "2-ng-c"
                    white_intersection_cooldown = 0
                    intersection_state_debug = ["2-ng-c", time.time()]
            else:
                # If none of the cutter points are at the bottom of the screen, and current_linefollowing_state has an "-ex" in it (exiting something), or we were just in a 3/4-way intersection
                # then we gotta keep cutting that line until we are fully out
                if (left_cutter_points[1][1] < img0_binary.shape[0]-3 or right_cutter_points[1][1] < img0_binary.shape[0]-3 
                    and ("-ex" in current_linefollowing_state or "3-ng" in current_linefollowing_state or "4-ng" in current_linefollowing_state)
                ):
                    if "3-ng" in current_linefollowing_state:
                        current_linefollowing_state = "2-ng-3-ex"
                    elif "4-ng" in current_linefollowing_state:
                        current_linefollowing_state = "2-ng-4-ex"
                    img0_line_new = helper_intersections.CutMaskWithLine(left_cutter_points[1], left_cutter_points[0], img0_line_new, "right")
                    img0_line_new = cv2.bitwise_not(helper_intersections.CutMaskWithLine(right_cutter_points[0], right_cutter_points[1], img0_line_new, "left"))
                    changed_black_contour = img0_line_new
                    intersection_state_debug = ["2-ng-h", time.time()]
                # Both cutter points are at the top or bottom of the screen, we have exited the intersection and just see a line
                elif (left_cutter_points[0][1] < 3 and right_cutter_points[0][1] < 3 or left_cutter_points[1][1] > img0_binary.shape[1]-3 and right_cutter_points[1][1] > img0_binary.shape[1]-3):
                    white_intersection_cooldown = 0
                    current_linefollowing_state = None
                    intersection_state_debug = ["2-ng-e", time.time()]
                # The average centre point of the left cutter points is to the left of the average centre point of the right cutter points, so we are doing a turn
                elif (centerOfLine(left_cutter_points)[0] < centerOfLine(right_cutter_points)[0]):
                    white_intersection_cooldown = 0
                    current_linefollowing_state = None
                    intersection_state_debug = ["2-ng-f", time.time()]
                # We are still in the intersection
                else:
                    intersection_state_debug = ["2-ng-g", time.time()]
                    img0_line_new = helper_intersections.CutMaskWithLine(left_cutter_points[1], left_cutter_points[0], img0_line_new, "right")
                    img0_line_new = cv2.bitwise_not(helper_intersections.CutMaskWithLine(right_cutter_points[0], right_cutter_points[1], img0_line_new, "left"))
                    changed_black_contour = img0_line_new

        if (len(white_contours_filtered) == 3):
            # We are entering a 3-way intersection
            if not current_linefollowing_state or "2-ng" in current_linefollowing_state:
                current_linefollowing_state = "3-ng-en"
            # We are exiting a 4-way intersection
            if "4-ng" in current_linefollowing_state:
                current_linefollowing_state = "3-ng-4-ex"
            
            intersection_state_debug = ["3-ng", time.time()]
            # Get the center of each contour
            white_contours_filtered_with_center = [(contour, centerOfContour(contour)) for contour in white_contours_filtered]

            # Sort the contours from left to right - Based on the centre of the contour's horz val
            sorted_contours_horz = sorted(white_contours_filtered_with_center, key=lambda contour: contour[1][0])

            # Simplify the contours to get the corner points
            approx_contours = [simplifiedContourPoints(contour[0], 0.03) for contour in sorted_contours_horz]

            # Middle of contour centres
            mid_point = (
                int(sum([contour[1][0] for contour in sorted_contours_horz])/len(sorted_contours_horz)),
                int(sum([contour[1][1] for contour in sorted_contours_horz])/len(sorted_contours_horz))
            )

            # Get the closest point of the approx contours to the mid point
            def closestPointToMidPoint(approx_contour, mid_point):
                return sorted(approx_contour, key=lambda point: pointDistance(point, mid_point))[0]

            # Get the closest points of each approx contour to the mid point, and store the index of the contour to back reference later
            closest_points = [
                [closestPointToMidPoint(approx_contour, mid_point), i] 
                for i, approx_contour in enumerate(approx_contours)
            ]
            
            # Get the closest points, sorted by distance to mid point
            sorted_closest_points = sorted(closest_points, key=lambda point: pointDistance(point[0], mid_point))
            closest_2_points_vert_sort = sorted(sorted_closest_points[:2], key=lambda point: point[0][1])

            # If a point is touching the top/bottom of the screen, it is quite possibly invalid and will cause some issues with cutting
            # So, we will find the next best point, the point inside the other contour that is at the top of the screen, and is closest to the X value of the other point
            for i, point in enumerate(closest_2_points_vert_sort):
                if point[0][1] > img0_line_new.shape[0]-10 or point[0][1] < 10:
                    # Find the closest point to the x value of the other point                    
                    other_point_x = closest_2_points_vert_sort[1-i][0][0]
                    other_point_approx_contour_i = closest_2_points_vert_sort[1-i][1]

                    closest_points_to_other_x = sorted(approx_contours[other_point_approx_contour_i], key=lambda point: abs(point[0] - other_point_x))
                    new_valid_points = [
                        point for point in closest_points_to_other_x 
                        if not np.isin(point, [
                            closest_2_points_vert_sort[0][0],
                            closest_2_points_vert_sort[1][0]
                        ]).any()
                    ]
                    if len(new_valid_points) == 0:
                        # print(f"Point {i} is at an edge, but no new valid points were found")
                        continue

                    closest_2_points_vert_sort = sorted([[new_valid_points[0], other_point_approx_contour_i], closest_2_points_vert_sort[1-i]], key=lambda point: point[0][1])
                    # print(f"Point {i} is at an edge, replacing with {new_valid_points[0]}")

            split_line = [point[0] for point in closest_2_points_vert_sort]
            
            contour_center_point_sides = [[], []] # Left, Right
            for i, contour in enumerate(sorted_contours_horz):
                if split_line[1][0] == split_line[0][0]:  # Line is vertical, so x is constant
                    side = "right" if contour[1][0] < split_line[0][0] else "left"
                else:
                    slope = (split_line[1][1] - split_line[0][1]) / (split_line[1][0] - split_line[0][0])
                    y_intercept = split_line[0][1] - slope * split_line[0][0]

                    if contour[1][1] < slope * contour[1][0] + y_intercept:
                        side = "left" if slope > 0 else "right"
                    else:
                        side = "right" if slope > 0 else "left"

                contour_center_point_sides[side == "left"].append(contour[1])
            
            # For the topmost contour in closest_2_points_vert_sort, find the edges that it touches
            def get_touching_edges(contour):
                edges = []
                for point in contour:
                    if point[0] == 0 and "left" not in edges:
                        edges.append("left")
                    if point[0] == img0_binary.shape[1]-1 and "right" not in edges:
                        edges.append("right")
                    if point[1] == 0 and "top" not in edges:
                        edges.append("top")
                    if point[1] == img0_binary.shape[0]-1 and "bottom" not in edges:
                        edges.append("bottom")
                return edges

            # Get the edges that the contour not relevant to the closest points touches
            edges_big = sorted(get_touching_edges(approx_contours[sorted_closest_points[2][1]]))

            # Cut direction is based on the side of the line with the most contour center points (contour_center_point_sides)
            cut_direction = len(contour_center_point_sides[0]) > len(contour_center_point_sides[1])

            # If we are just entering a 3-way intersection, and the 'big contour' does not connect to the bottom, 
            # we may be entering a 4-way intersection... so follow the vertical line
            if len(edges_big) >= 2 and "bottom" not in edges_big and "-en" in current_linefollowing_state:
                cut_direction = not cut_direction
            # We are exiting a 4-way intersection, so follow the vertical line
            elif current_linefollowing_state == "3-ng-4-ex":
                cut_direction = not cut_direction
            else:
                # We have probably actually entered now, lets stop following the vert line and do the normal thing.
                current_linefollowing_state = "3-ng"

                # If this is true, the line we want to follow is the smaller, perpendicular line to the large line.
                # This case should realistically never happen, but it's here just in case.
                if edges_big == ["bottom", "left", "right"] or edges_big == ["left", "right", "top"]:
                    cut_direction = not cut_direction
                # If the contour not relevant to the closest points is really small (area), we are probably just entering the intersection,
                # So we need to follow the line that is perpendicular to the large line
                # We ignore this if edges_big does not include the bottom, because we could accidently have the wrong contour in some weird angle
                elif cv2.contourArea(sorted_contours_horz[sorted_closest_points[2][1]][0]) < 7000 and "bottom" in edges_big:
                    cut_direction = not cut_direction

            # CutMaskWithLine will fail if the line is flat, so we need to make sure that the line is not flat
            if closest_2_points_vert_sort[0][0][1] == closest_2_points_vert_sort[1][0][1]:
                closest_2_points_vert_sort[0][0][1] += 1 # Move the first point up by 1 pixel
                
            img0_line_new = helper_intersections.CutMaskWithLine(closest_2_points_vert_sort[0][0], closest_2_points_vert_sort[1][0], img0_line_new, "left" if cut_direction else "right")
            changed_black_contour = cv2.bitwise_not(img0_line_new)

        if (len(white_contours_filtered) == 4):
            intersection_state_debug = ["4-ng", time.time()]
            # Get the center of each contour
            white_contours_filtered_with_center = [(contour, centerOfContour(contour)) for contour in white_contours_filtered]

            # Sort the contours from left to right - Based on the centre of the contour's horz val
            sorted_contours_horz = sorted(white_contours_filtered_with_center, key=lambda contour: contour[1][0])
            
            # Sort the contours from top to bottom, for each side of the image - Based on the centre of the contour's vert val
            contour_BL, contour_TL = tuple(sorted(sorted_contours_horz[:2], reverse=True, key=lambda contour: contour[1][1]))
            contour_BR, contour_TR = tuple(sorted(sorted_contours_horz[2:], reverse=True, key=lambda contour: contour[1][1]))

            # Simplify the contours to get the corner points
            approx_BL = simplifiedContourPoints(contour_BL[0], 0.03)
            approx_TL = simplifiedContourPoints(contour_TL[0], 0.03)
            approx_BR = simplifiedContourPoints(contour_BR[0], 0.03)
            approx_TR = simplifiedContourPoints(contour_TR[0], 0.03)

            # Middle of contour centres
            mid_point = (
                int((contour_BL[1][0] + contour_TL[1][0] + contour_BR[1][0] + contour_TR[1][0]) / 4),
                int((contour_BL[1][1] + contour_TL[1][1] + contour_BR[1][1] + contour_TR[1][1]) / 4)
            )

            # Get the closest point of the approx contours to the mid point
            def closestPointToMidPoint(approx_contour, mid_point):
                return sorted(approx_contour, key=lambda point: pointDistance(point, mid_point))[0]
            
            closest_BL = closestPointToMidPoint(approx_BL, mid_point)
            closest_TL = closestPointToMidPoint(approx_TL, mid_point)
            closest_BR = closestPointToMidPoint(approx_BR, mid_point)
            closest_TR = closestPointToMidPoint(approx_TR, mid_point)

            # If closest_TL or closest_TR is touching the top of the screen, it is quite possibly invalid and will cause some issues with cutting
            # So, we will find the next best point, the point inside the relevant contour, and is closest to the X value of the other point
            if closest_TL[1] < 10:
                closest_TL = closest_BL
                closest_BL = sorted(approx_BL, key=lambda point: abs(point[0] - closest_BL[0]))[1]
            elif closest_BL[1] > img0_binary.shape[0] - 10:
                closest_BL = closest_TL
                closest_TL = sorted(approx_TL, key=lambda point: abs(point[0] - closest_TL[0]))[1]
            # # We will do the same with the right-side contours
            if closest_TR[1] < 10:
                closest_TR = closest_BR
                closest_BR = sorted(approx_BR, key=lambda point: abs(point[0] - closest_BR[0]))[1]
            elif closest_BR[1] > img0_binary.shape[0] - 10:
                closest_BR = closest_TR
                closest_TR = sorted(approx_TR, key=lambda point: abs(point[0] - closest_TR[0]))[1]

            img0_line_new = helper_intersections.CutMaskWithLine(closest_BL, closest_TL, img0_line_new, "left")
            img0_line_new = helper_intersections.CutMaskWithLine(closest_BR, closest_TR, img0_line_new, "right")

            current_linefollowing_state = "4-ng"
            changed_black_contour = cv2.bitwise_not(img0_line_new)

    if (changed_black_contour is not False):
        print("Changed black contour, LF State: ", current_linefollowing_state)
        cv2.drawContours(img0, black_contours, -1, (0,0,255), 2)
        new_black_contours, new_black_hierarchy = cv2.findContours(changed_black_contour, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        if (len(new_black_contours) > 0):
            black_contours = new_black_contours
            black_hierarchy = new_black_hierarchy
        else:
            print("No black contours found after changing contour")

        changed_black_contour = False

    # -----------
    # REST OF LINE LINE FOLLOWER
    # -----------

    #Find the black contours
    sorted_black_contours = FindBestContours(black_contours)
    if (len(sorted_black_contours) == 0):
        print("No black contours found")

        print("STEER TEMP: GO FORWARD")
        

        preview_image_img0 = cv2.resize(img0, (0,0), fx=0.8, fy=0.7)
        cv2.imshow("img0", preview_image_img0)
        k = cv2.waitKey(1)
        if (k & 0xFF == ord('q')):
            # pr.print_stats(SortKey.TIME)
            program_active = False
            break
        continue
    chosen_black_contour = sorted_black_contours[0]
    
    # Update the reference position for subsequent calculations
    last_line_pos = np.array([chosen_black_contour[1][0][0], chosen_black_contour[1][0][1]])

    # Retrieve the four courner points of the chosen contour
    black_bounding_box = np.intp(cv2.boxPoints(chosen_black_contour[1]))

    # Error (distance from the center of the image) and angle (of the line) of the chosen contour
    black_contour_error = int(last_line_pos[0] - (img0.shape[1]/2))
    black_contour_angle = int(chosen_black_contour[1][2])

    # Sort the black bounding box points based on their y-coordinate (bottom to top)
    vert_sorted_black_bounding_points = sorted(black_bounding_box, key=lambda point: -point[1])

    # Find leftmost line points based on splitting the bounding box into two vertical halves
    black_leftmost_line_points = [
        sorted(vert_sorted_black_bounding_points[:2], key=lambda point: point[0])[0], # Left-most point of the top two points
        sorted(vert_sorted_black_bounding_points[2:], key=lambda point: point[0])[0]  # Left-most point of the bottom two points
    ]
    
    # The two top-most points, sorted from left to right
    horz_sorted_black_bounding_points_top_2 = sorted(vert_sorted_black_bounding_points[:2], key=lambda point: point[0])
    
    bigTurnMargin = 30
    # If the angle of the contour is big enough and the contour is close to the edge of the image (within bigTurnMargin pixels)
    # Then, the line likely is a big turn and we need to turn more
    isBigTurn = (
        black_contour_angle > 85 
        and (
            horz_sorted_black_bounding_points_top_2[0][0] < bigTurnMargin  # If the leftmost point is close (bigTurnMargin) to the left side of the image
            or 
            horz_sorted_black_bounding_points_top_2[1][0] > img0.shape[0] - bigTurnMargin # 
        )
    )

    # cv2.line(img0, (horz_sorted_black_bounding_points_top_2[0][0], 0), (horz_sorted_black_bounding_points_top_2[0][0], img0.shape[1]), (255, 255, 0), 3)
    # cv2.line(img0, (horz_sorted_black_bounding_points_top_2[1][0], 0), (horz_sorted_black_bounding_points_top_2[1][0], img0.shape[1]), (255, 125, 0), 3)


    black_contour_angle_new = black_contour_angle
    # If       the top left point is to the right of the bottom left point
    #  or, if  the contour angle is above 80 and the last angle is close to 0 (+- 5)
    #  or, if  the contour angle is above 80 and the current angle is close to 0 (+- 2) (bottom left point X-2 < top left point X < bottom left point X+2)
    # Then, the contour angle is probably 90 degrees off what we want it to be, so subtract 90 degrees from it
    # 
    # This does not apply if the line is a big turn
    if (
        not isBigTurn
        and (
            black_leftmost_line_points[0][0] > black_leftmost_line_points[1][0]
            or (
                -5 < last_ang < 5 
                and black_contour_angle_new > 80
            ) 
            or (
                black_leftmost_line_points[1][0]-2 < black_leftmost_line_points[0][0] < black_leftmost_line_points[1][0]+2 
                and black_contour_angle_new > 80
            )
        )
    ):
        black_contour_angle_new = black_contour_angle_new-90
    

    black_x, black_y, black_w, black_h = cv2.boundingRect(chosen_black_contour[2])

    # If the contour angle is above 70 and the line is at the edge of the image, then flip the angle
    if (black_contour_angle_new > 70 and black_x == 0 or black_contour_angle_new < -70 and black_x > img0.shape[1]-5):
        black_contour_angle_new = black_contour_angle_new*-1
        changed_angle = True

    # If we haven't already changed the angle, 
    #   and if the contour angle is above 80 and the last angle is below -20
    #   or  if the contour angle is below -80 and the last angle is above 20 
    # Then flip the angle
    #
    # This is to catch the case where the angle has flipped to the other side and needs to be flipped back
    if (
        not changed_angle 
        and (
            (black_contour_angle_new > 80 and last_ang < -20) 
            or (black_contour_angle_new < -80 and last_ang > 20)
        )
    ):
        black_contour_angle_new = black_contour_angle_new*-1
        changed_ang = True
    
    last_ang = black_contour_angle_new

    #Motor stuff
    current_position = (black_contour_angle_new/max_angle)*angle_weight+(black_contour_error/max_error)*error_weight
    current_position *= 100
    
    current_steering = pid(-current_position)
    
    if time.time()-delay > 2:
        motor_vals = m.run_steer(follower_speed, 100, current_steering)
        print(f"Steering: {int(current_steering)} \t{str(motor_vals)}")
    elif time.time()-delay <= 4:
        print(f"DELAY {4-time.time()+delay}")



    cv2.drawContours(img0, [chosen_black_contour[2]], -1, (0,255,0), 3) # DEBUG
    # cv2.drawContours(img0, [black_bounding_box], 0, (255, 0, 255), 2)
    cv2.line(img0, black_leftmost_line_points[0], black_leftmost_line_points[1], (255, 20, 51, 0.5), 3)

    preview_image_img0 = cv2.resize(img0, (0,0), fx=0.8, fy=0.7)
    cv2.imshow("img0", preview_image_img0)

    # preview_image_img0_binary = cv2.resize(img0_binary, (0,0), fx=0.8, fy=0.7)
    # cv2.imshow("img0_binary", preview_image_img0_binary)

    preview_image_img0_line = cv2.resize(img0_line, (0,0), fx=0.8, fy=0.7)
    cv2.imshow("img0_line", preview_image_img0_line)

    preview_image_img0_green = cv2.resize(img0_green, (0,0), fx=0.8, fy=0.7)
    cv2.imshow("img0_green", preview_image_img0_green)

    # preview_image_img0_gray = cv2.resize(img0_gray, (0,0), fx=0.8, fy=0.7)
    # cv2.imshow("img0_gray", preview_image_img0_gray)

    # def mouseCallbackHSV(event, x, y, flags, param):
    #     if event == cv2.EVENT_MOUSEMOVE and flags == cv2.EVENT_FLAG_LBUTTON:
    #         # Print HSV value only when the left mouse button is pressed and mouse is moving
    #         hsv_value = img0_hsv[y, x]
    #         print(f"HSV: {hsv_value}")
    # # Show HSV preview with text on hover to show HSV values
    # preview_image_img0_hsv = cv2.resize(img0_hsv, (0,0), fx=0.8, fy=0.7)
    # cv2.imshow("img0_hsv", preview_image_img0_hsv)
    # cv2.setMouseCallback("img0_hsv", mouseCallbackHSV)

    # preview_image_img0_gray_scaled = cv2.resize(img0_gray_scaled, (0,0), fx=0.8, fy=0.7)
    # cv2.imshow("img0_gray_scaled", preview_image_img0_gray_scaled)

    # Show a preview of the image with the contours drawn on it, black as red and white as blue

    preview_image_img0_contours = img0_clean.copy()
    cv2.drawContours(preview_image_img0_contours, white_contours, -1, (255,0,0), 3)
    cv2.drawContours(preview_image_img0_contours, black_contours, -1, (0,255,0), 3)
    cv2.drawContours(preview_image_img0_contours, [chosen_black_contour[2]], -1, (0,0,255), 3)
    
    cv2.putText(preview_image_img0_contours, f"{black_contour_angle:4d} Angle Raw", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2) # DEBUG
    cv2.putText(preview_image_img0_contours, f"{black_contour_angle_new:4d} Angle", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2) # DEBUG
    cv2.putText(preview_image_img0_contours, f"{black_contour_error:4d} Error", (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2) # DEBUG
    cv2.putText(preview_image_img0_contours, f"{int(current_position):4d} Position", (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2) # DEBUG
    cv2.putText(preview_image_img0_contours, f"{int(current_steering):4d} Steering", (10, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2) # DEBUG

    if isBigTurn:
        cv2.putText(preview_image_img0_contours, f"Big Turn", (10, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

    cv2.putText(preview_image_img0_contours, f"LF State: {current_linefollowing_state}", (10, 240), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)
    cv2.putText(preview_image_img0_contours, f"INT Debug: {intersection_state_debug[0]} - {int(time.time() - intersection_state_debug[1])}", (10, 270), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 255), 2)

    preview_image_img0_contours = cv2.resize(preview_image_img0_contours, (0,0), fx=0.8, fy=0.7)
    cv2.imshow("img0_contours", preview_image_img0_contours)

    # frames += 1

    k = cv2.waitKey(1)
    if (k & 0xFF == ord('q')):
        # pr.print_stats(SortKey.TIME)
        program_active = False
        break

cams.stop()
cv2.destroyAllWindows()