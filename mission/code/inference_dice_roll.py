import subprocess
import os
from datetime import datetime
import json
import time

# --- LEROBOT IMPORTS AND CONFIGURATION FOR HOMING ---

# Attempt to import necessary motor modules (This prevents AttributeErrors previously seen)
try:
    from lerobot.motors.feetech import FeetechMotorsBus
    from lerobot.motors.motors_bus import Motor, MotorNormMode
except ImportError:
    # Define dummy classes if the motors library is not importable (fallback)
    class Dummy:
        def __init__(self, *args, **kwargs): pass
        def connect(self): print("Homing failed: lerobot motors not found.")
        def disconnect(self): pass
        def write(self, *args): pass
    FeetechMotorsBus = Dummy
    Motor = Dummy
    MotorNormMode = Dummy
    
# --- ROBOT CONFIGURATION ---
ROBOT_PORT = "/dev/ttyACM1" 
CALIBRATION_PATH = "/home/team19/.cache/huggingface/lerobot/calibration/robots/so101_follower/my_follower_arm.json"

MOTORS = {
    "shoulder_pan": Motor(id=1, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "shoulder_lift": Motor(id=2, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "elbow_flex": Motor(id=3, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "wrist_flex": Motor(id=4, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "wrist_roll": Motor(id=5, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "gripper": Motor(id=6, model="sts3215", norm_mode=MotorNormMode.RANGE_0_100),
}

HOME_POSITION = {
    "shoulder_pan": -9.01,
    "shoulder_lift": -100.13,
    "elbow_flex": 90.95,
    "wrist_flex": 22.42,
    "wrist_roll": -87.08,
    "gripper": 2.34 # Gripper mostly closed
}

# Helper class to satisfy the motor bus's calibration requirements
class CalibrationData:
    def __init__(self, data):
        self.id = data.get('id')
        self.drive_mode = data.get('drive_mode')
        self.homing_offset = data.get('homing_offset')
        self.range_min = data.get('range_min') 
        self.range_max = data.get('range_max')
        self.calib_mode = "LINEAR" if self.id == 6 else "DEGREE"
        
def load_calibration(calib_path, motor_names):
    """Load calibration data from JSON file."""
    if not os.path.exists(calib_path): return None
    try:
        with open(calib_path, 'r') as f:
            calib_data = json.load(f)
        calibration = {}
        for motor_name in motor_names:
            if motor_name in calib_data:
                calibration[motor_name] = CalibrationData(calib_data[motor_name])
        return calibration
    except Exception: return None

def home_robot(home_pos, port, motors, calib_path):
    """Initializes the robot bus, moves to home position, and disconnects."""
    print("\n--- Moving robot to Home Position ---")
    bus = None
    try:
        # Load the calibration data needed for the motor bus
        calibration = load_calibration(calib_path, list(motors.keys()))
        bus = FeetechMotorsBus(port=port, motors=motors, calibration=calibration)
        bus.connect()
        
        # Write the home position goals for all motors
        for motor_name, position in home_pos.items():
            if motor_name in motors:
                # Use "Goal_Position" to set the final target
                bus.write("Goal_Position", motor_name, position)
        
        # Wait for the robot to reach the home position (3 seconds)
        time.sleep(3.0) 

    except Exception as e:
        print(f"Error during homing: {e}. Check robot connection.")
    finally:
        if bus and hasattr(bus, 'disconnect'):
            bus.disconnect()
            print("Robot bus disconnected.")


# --- MAIN EXECUTION ---

# Generate unique dataset root name with timestamp
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
dataset_root = f"{os.getcwd()}/eval_smolvla7_{timestamp}/"

cmd = [
    "lerobot-record",
    "--robot.type=so101_follower",
    "--robot.port=/dev/ttyACM1",
    "--robot.id=my_follower_arm",
    '--robot.cameras={"camera1": {"type": "opencv", "index_or_path": 4, "width": 640, "height": 480, "fps": 30, "fourcc": "MJPG"}, "camera2": {"type": "opencv", "index_or_path": 0, "width": 640, "height": 480, "fps": 30, "fourcc": "MJPG"}, "camera3": {"type": "opencv", "index_or_path": 6, "width": 640, "height": 480, "fps": 30, "fourcc": "MJPG"}}',
    "--dataset.single_task=Pick up die, shake it, and drop in zone",
    "--dataset.repo_id=mutterehman/eval_dice-rolling-v2",
    f"--dataset.root={dataset_root}",
    "--dataset.episode_time_s=50",
    "--dataset.num_episodes=1",
    "--policy.path=/home/team19/nsmolvla",
]

print(f"Recording to: {dataset_root}")
print("Starting lerobot-record command. Waiting for command to complete...")

# Execute the recording command - script waits here until recording is done
subprocess.run(cmd)

# --- HOME ROBOT AFTER RECORDING IS COMPLETE ---
home_robot(HOME_POSITION, ROBOT_PORT, MOTORS, CALIBRATION_PATH)