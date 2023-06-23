import time
import cv2
import json
import numpy as np
import queue
from picamera2 import Picamera2
from threading import Thread

def get_camera(num):
    cam = Picamera2(num)
    if num == 0: # Pi camera
        available_modes = cam.sensor_modes
        available_modes.sort(key=lambda x: x["fps"], reverse=True)
        chosen_mode = available_modes[0]
        cam.video_configuration = cam.create_video_configuration(
                raw={"size": chosen_mode["size"], "format": chosen_mode["format"].format},
                main={"size": (640,480)},
        )
        cam.configure("video")
        cam.set_controls({"FrameRate": 120,"ExposureTime": 10000})

    return cam

class CameraController:
    def __init__(self, processing_conf=None):
        self.cameras = [
            CameraStream(0, processing_conf)
        ]

    def stop(self):
        for cam in self.cameras:
            cam.stop_stream()

    def read_stream(self, num):
        return self.cameras[num].read_stream()
    
    def read_stream_processed(self, num):
        return self.cameras[num].read_stream_processed()

    def start_stream(self, num):
        self.cameras[num].start_stream()

    def process_frame(self, num):
        self.cameras[num].process_frame()

    def stop_stream(self, num):
        self.cameras[num].stop_stream()

    def get_fps(self, num):
        return self.cameras[num].get_fps()
    
    def is_halted(self, num):
        return self.cameras[num].buffer_halt

class CameraStream:
    def __init__(self, num=0, processing_conf=None):
        self.num = num
        self.cam = get_camera(self.num)
        self.processing_conf = processing_conf

        if self.processing_conf is None:
            print("Warning: No processing configuration provided, images will not be pre-processed")

        self.buffer_halt = False

        self.buffer_queue = queue.Queue(maxsize=1)
        self.buffer_thread = None
        self.buffer_thread_id = 0
        self.last_buffer_create_time = 0

        self.first_frame_found = False

        self.stream_running = False
        
        self.frame = None


        self.processed = {
            "raw": None,
            "resized": None,
            "gray": None,
            "gray_scaled": None,
            "binary": None,
            "hsv": None,
            "green": None,
            "line": None
        }

        self.frames = 0
        self.start_time = 0
        self.last_capture_time = 0
        self.stop_time = 0

    def take_image(self):
        self.cam.start()
        new_frame = self.frame
        while new_frame == self.frame:
            new_frame = self.cam.helpers.make_array(self.cam.capture_buffer(), self.cam.camera_configuration()["main"])
            print(f"C{self.num} Frame Captured")
        self.cam.stop()
        self.frame = new_frame
        return self.frame
        
    def read_stream(self):
        if not self.stream_running: 
            raise Exception(f"Camera {self.num} is not active, run .start_stream() first")

        return self.frame

    def read_stream_processed(self):
        if not self.stream_running: 
            raise Exception(f"Camera {self.num} is not active, run .start_stream() first")

        return self.processed

    def start_stream(self):
        print(f"[CAMERA] Starting stream for Camera {self.num}")
        self.thread = Thread(target=self.update_stream, args=())
        self.stream_running = True
        self.thread.start()
        
    def update_stream(self):
        self.cam.start()
        self.start_time = time.time()
        self.last_capture_time = time.time()

        while self.stream_running:
            self.frames += 1
            
            if not self.buffer_thread or not self.buffer_thread.is_alive():
                self.buffer_halt = True
                print("\nNEW BUFFER CREATED\n")
                if time.time() - self.last_buffer_create_time < 5:
                    print("WARNING: Buffer thread died too quickly, entirely restarting stream")
                    try:
                        def thread_attempt_stop():
                            self.cam.stop()
                            print("Camera stopped")
                        def thread_attempt_close():
                            self.cam.close()
                            print("Camera closed")

                        threadStop = Thread(target=thread_attempt_stop, args=())
                        threadClose = Thread(target=thread_attempt_close, args=())
                        
                        # Allow 1 second max for each thread to stop
                        threadStop.start()
                        threadStop.join(1)
                        print("Camera stop attempted")
                        threadClose.start()
                        threadClose.join(1)
                        print("Camera close attempted")
                    except Exception as e:
                        print("Error stopping camera, will continue anyway")
                        print(e)
                    
                    self.cam = get_camera(self.num)
                    self.cam.start()
                    print("Camera restarted, continuing", time.time())

                    time.sleep(100)
                    
                self.buffer_thread_id += 1
                self.first_frame_found = False
                self.buffer_thread = Thread(target=self.capture_buffer_thread, args=(self.buffer_thread_id,))
                self.buffer_thread.start()
                self.last_capture_time = time.time()
                self.last_buffer_create_time = time.time()

            try:
                buf = self.buffer_queue.get(timeout=0.5)
                self.frame = self.cam.helpers.make_array(buf, self.cam.camera_configuration()["main"])

                self.first_frame_found = True
                self.last_capture_time = time.time()

                if self.processing_conf is not None:
                    self.process_frame()
                self.buffer_halt = False
            except queue.Empty:
                print("Buffer capture timed out. Skipping frame")

            # Check if no buffer has been added for at least 1 second
            if time.time() - self.last_capture_time > (1 if self.first_frame_found else 3):
                print("WARNING: No buffer added for 1 second, camera stream may be frozen - restarting stream")
                self.buffer_thread = None
                self.buffer_halt = True

        self.cam.stop()
        self.stop_time = time.time()
    
    def capture_buffer_thread(self, thread_id):
        print(f"Created buffer thread #{thread_id}")

        while self.stream_running:
            if thread_id != self.buffer_thread_id:
                print(f"Buffer thread #{thread_id} is no longer needed, exiting")
                break

            buf = self.cam.capture_buffer()
            
            # Clear the queue
            while not self.buffer_queue.empty():
                self.buffer_queue.get_nowait()
            
            self.buffer_queue.put(buf)
        
        print(f"Buffer thread #{thread_id} has exited")


    def process_frame(self):
        frame = self.frame
        if frame is None:
            raise Exception(f"Camera {self.num} has no frame to process, run .take_image() first")
        
        if self.processing_conf is None:
            raise Exception(f"Camera {self.num} has no conf for processing")

        resized = frame[0:429, 0:frame.shape[1]]
        resized = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        # Find the black in the image
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)

        # Scale white values based on the inverse of the calibration map
        gray_scaled = self.processing_conf["calibration_map"] * gray

        # Get the binary image
        black_line_threshold = self.processing_conf["black_line_threshold"]
        binary = ((gray_scaled > black_line_threshold) * 255).astype(np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((7,7),np.uint8))

        # Find green in the image
        hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
        green_turn_hsv_threshold = self.processing_conf["green_turn_hsv_threshold"]
        green = cv2.bitwise_not(cv2.inRange(hsv, green_turn_hsv_threshold[0], green_turn_hsv_threshold[1]))
        green = cv2.erode(green, np.ones((5,5),np.uint8), iterations=1)

        # Find the line, by removing the green from the image (since green looks like black when grayscaled)
        line = cv2.dilate(binary, np.ones((5,5),np.uint8), iterations=2)
        line = cv2.bitwise_or(line, cv2.bitwise_not(green))

        # Only set the processed data once it is all populated, to avoid partial data being read
        self.processed = {
            "raw": frame,
            "resized": resized,
            "gray": gray,
            "gray_scaled": gray_scaled,
            "binary": binary,
            "hsv": hsv,
            "green": green,
            "line": line
        }

    def stop_stream(self):
        print(f"[CAMERA] Stopping stream for Camera {self.num}")
        self.stream_running = False

    def get_fps(self):
        return int(self.frames/(time.time() - self.start_time))

if __name__ == "__main__":
    camNum = 0
    cams = CameraController()
    cams.start_stream(camNum)
    time.sleep(1)
    
    while True:
        img = cams.read_stream(camNum)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = cv2.resize(img, (0,0), fx=0.5, fy=0.5)

        gamma = 1.3
        gammatable = [((i / 255) ** (1 / gamma)) * 255 for i in range(256)]
        gammatable = np.array(gammatable, np.uint8)
        img = cv2.LUT(img, gammatable)

        cv2.imshow(f"Camera {camNum}", img)

        k = cv2.waitKey(1)
        if (k & 0xFF == ord('q')):
            break
    
    print(f"Average FPS: {cams.get_fps(camNum)}")
    cams.stop()
