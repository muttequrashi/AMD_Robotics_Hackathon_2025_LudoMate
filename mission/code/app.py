from flask import Flask, render_template, jsonify, request, Response
from flask_cors import CORS
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.motors.motors_bus import Motor, MotorNormMode
import cv2
import time
import json
import numpy as np
import threading
import traceback
import os 
import subprocess
app = Flask(__name__)
CORS(app)

# --- Robot Configuration ---
ROBOT_PORT = "/dev/ttyACM1"
ROBOT_ID = "my_follower_arm"
CALIBRATION_PATH = "/home/team19/.cache/huggingface/lerobot/calibration/robots/so101_follower/my_follower_arm.json"

MOTORS = {
    "shoulder_pan": Motor(id=1, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "shoulder_lift": Motor(id=2, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "elbow_flex": Motor(id=3, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "wrist_flex": Motor(id=4, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "wrist_roll": Motor(id=5, model="sts3215", norm_mode=MotorNormMode.DEGREES),
    "gripper": Motor(id=6, model="sts3215", norm_mode=MotorNormMode.RANGE_0_100),
}
MOTOR_NAMES = list(MOTORS.keys())

HOME_POSITION = {
    "shoulder_pan": -9.01,
    "shoulder_lift": -100.13,
    "elbow_flex": 90.95,
    "wrist_flex": 22.42,
    "wrist_roll": -87.08,
    "gripper": 2.34
}

# --- Camera Configuration ---
CAMERAS = {
    'top': {'index': 4, 'name': 'Top View'},
    'gripper': {'index': 6, 'name': 'Gripper View'},
    'front': {'index': 0, 'name': 'Front View'}
}

# Global variables
motor_bus = None
bus_lock = threading.Lock()
robot_initialized = False
camera_captures = {}
camera_lock = threading.Lock()

STEP_SIZE = 10.0
GRIPPER_STEP = 20.0

# Saved positions storage
saved_positions = {}
POSITIONS_FILE = "saved_positions.json"

# --- Camera Functions ---
def initialize_cameras():
    """Initialize all camera captures"""
    global camera_captures
    
    for cam_id, cam_info in CAMERAS.items():
        try:
            cap = cv2.VideoCapture(cam_info['index'])
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 30)
            
            if cap.isOpened():
                camera_captures[cam_id] = cap
                print(f"✅ Camera '{cam_id}' initialized on index {cam_info['index']}")
            else:
                print(f"❌ Failed to open camera '{cam_id}' on index {cam_info['index']}")
        except Exception as e:
            print(f"❌ Error initializing camera '{cam_id}': {e}")

def generate_frames(camera_id):
    """Generate frames for video streaming"""
    global camera_captures
    
    if camera_id not in camera_captures:
        return
    
    cap = camera_captures[camera_id]
    
    while True:
        with camera_lock:
            success, frame = cap.read()
        
        if not success:
            time.sleep(0.1)
            continue
        
        # Encode frame
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ret:
            continue
        
        frame_bytes = buffer.tobytes()
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')

# --- Robot Functions (Keep your existing ones) ---
class CalibrationData:
    def __init__(self, motor_name, data):
        self.id = data.get('id')
        self.drive_mode = data.get('drive_mode')
        self.homing_offset = data.get('homing_offset')
        self.range_min = data.get('range_min')
        self.range_max = data.get('range_max')
        self.calib_mode = "LINEAR" if motor_name == "gripper" else "DEGREE"

def load_calibration(calib_path, motor_names):
    print(f"Loading calibration from: {calib_path}")
    try:
        with open(calib_path, 'r') as f:
            calib_data = json.load(f)
        
        calibration = {}
        for motor_name in motor_names:
            if motor_name in calib_data:
                calibration[motor_name] = CalibrationData(motor_name, calib_data[motor_name])
        return calibration
    except FileNotFoundError:
        print("Calibration file not found!")
        return None

def create_motor_bus(port, motors, calibration):
    print(f"Creating motor bus on port: {port}")
    bus = FeetechMotorsBus(port=port, motors=motors, calibration=calibration)
    print("Connecting to motor bus...")
    bus.connect()
    print("Motor bus connected successfully!")
    return bus

def initialize_robot():
    global motor_bus, robot_initialized
    try:
        print("Initializing robot...")
        calibration = load_calibration(CALIBRATION_PATH, MOTOR_NAMES)
        motor_bus = create_motor_bus(ROBOT_PORT, MOTORS, calibration)
        robot_initialized = True
        print("Robot initialized successfully!")
        return True
    except Exception as e:
        print(f"Failed to initialize robot: {e}")
        traceback.print_exc()
        robot_initialized = False
        return False

def read_all_motor_positions(bus, motor_names, register="Present_Position"):
    results = []
    for name in motor_names:
        pos = bus.read(register, name)
        if isinstance(pos, (list, np.ndarray)):
            results.append(pos[0])
        else:
            results.append(pos)
    return np.array(results)

def move_to_position_smooth(target_positions, duration=1.0):
    global motor_bus
    if not robot_initialized or motor_bus is None:
        raise Exception("Robot not initialized")
    
    with bus_lock:
        motor_names_to_move = [name for name in target_positions.keys() if name in MOTOR_NAMES]
        if not motor_names_to_move:
            return
        
        starting_positions_array = read_all_motor_positions(motor_bus, motor_names_to_move)
        starting_positions = {name: pos for name, pos in zip(motor_names_to_move, starting_positions_array)}
        
        start_time = time.time()
        while True:
            elapsed = time.time() - start_time
            if elapsed >= duration:
                break
            
            alpha = min(elapsed / duration, 1.0)
            for motor_name in motor_names_to_move:
                start_pos = starting_positions.get(motor_name, 0)
                target_pos = target_positions.get(motor_name, start_pos)
                current_pos = (1 - alpha) * start_pos + alpha * target_pos
                motor_bus.write("Goal_Position", motor_name, current_pos)
            
            time.sleep(0.02)
        
        for motor_name in motor_names_to_move:
            motor_bus.write("Goal_Position", motor_name, target_positions[motor_name])

# --- Load/Save Positions ---
def load_saved_positions():
    global saved_positions
    try:
        with open(POSITIONS_FILE, 'r') as f:
            saved_positions = json.load(f)
        print(f"Loaded {len(saved_positions)} saved positions")
    except FileNotFoundError:
        saved_positions = {}
        print("No saved positions file found")

def save_positions_to_file():
    with open(POSITIONS_FILE, 'w') as f:
        json.dump(saved_positions, f, indent=2)

# --- Flask Routes ---
@app.route('/')
def index():
    return render_template('index.html', cameras=CAMERAS)

@app.route('/video_feed/<camera_id>')
def video_feed(camera_id):
    """Video streaming route"""
    if camera_id not in CAMERAS:
        return "Camera not found", 404
    
    return Response(generate_frames(camera_id),
                   mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        'initialized': robot_initialized,
        'port': ROBOT_PORT,
        'home_position': HOME_POSITION,
        'motors': MOTOR_NAMES,
        'cameras': list(CAMERAS.keys())
    })
@app.route('/api/auto_dice_roll', methods=['POST'])
def auto_dice_roll():
    """Disconnect from robot AND cameras, run AI inference, then reconnect everything"""
    global motor_bus, robot_initialized, camera_captures
    
    if not robot_initialized or motor_bus is None:
        return jsonify({'error': 'Robot not initialized'}), 500
    
    try:
        def run_ai_inference():
            import time
            import subprocess
            
            # Step 1: Move to home position first (while we still have control)
            print("🏠 Moving to home position...")
            try:
                move_to_position_smooth(HOME_POSITION.copy(), duration=3.0)
                time.sleep(3.5)  # Wait for movement to complete
            except Exception as e:
                print(f"Error moving to home: {e}")
            
            # Step 2: Disconnect CAMERAS
            print("📹 Releasing all cameras...")
            global camera_captures
            
            with camera_lock:
                for cam_id, cap in camera_captures.items():
                    try:
                        cap.release()
                        print(f"  ✅ Released camera: {cam_id}")
                    except Exception as e:
                        print(f"  ⚠️ Error releasing {cam_id}: {e}")
                
                camera_captures.clear()
                print("✅ All cameras released")
            
            time.sleep(1)  # Give cameras time to fully release
            
            # Step 3: Disconnect MOTORS
            print("🔌 Disconnecting motor bus from Flask server...")
            global motor_bus, robot_initialized
            
            with bus_lock:
                try:
                    motor_bus.disconnect()
                    print("✅ Motor bus disconnected")
                except Exception as e:
                    print(f"Warning during disconnect: {e}")
                
                motor_bus = None
                robot_initialized = False
            
            # Wait to ensure everything is released
            time.sleep(2)
            
            # Step 4: Run the AI inference script
            print("🎲 Starting AI inference script...")
            print("=" * 60)
            
            script_path = os.path.join(os.path.dirname(__file__), '..', 'inference_dice_roll.py')
            script_path = os.path.abspath(script_path)
            
            if not os.path.exists(script_path):
                print(f"❌ Script not found: {script_path}")
                # Reconnect everything even if script not found
                time.sleep(2)
                reconnect_everything()
                return
            
            try:
                # Run the inference script
                print(f"▶️ Running: python {script_path}")
                print(f"📁 Working directory: {os.path.dirname(script_path)}")
                print("=" * 60)
                
                result = subprocess.run(
                    ['python', script_path],
                    capture_output=True,
                    text=True,
                    timeout=180,  # 3 minute timeout
                    cwd=os.path.dirname(script_path)
                )
                
                print("=" * 60)
                print("🤖 INFERENCE OUTPUT:")
                print("=" * 60)
                print(result.stdout)
                print("=" * 60)
                
                if result.stderr:
                    print("⚠️ INFERENCE STDERR:")
                    print("=" * 60)
                    print(result.stderr)
                    print("=" * 60)
                
                if result.returncode == 0:
                    print("✅ Inference completed successfully!")
                else:
                    print(f"⚠️ Inference exited with code {result.returncode}")
                
            except subprocess.TimeoutExpired:
                print("⏱️ Inference script timed out after 3 minutes")
            except Exception as e:
                print(f"❌ Error running inference: {e}")
                traceback.print_exc()
            
            # Step 5: Wait before reconnecting
            print("⏳ Waiting 3 seconds before reconnecting...")
            time.sleep(3)
            
            # Step 6: Reconnect everything
            reconnect_everything()
        
        def reconnect_everything():
            """Reconnect cameras and motors"""
            print("🔄 Reconnecting everything...")
            
            # Reconnect cameras
            print("📹 Reconnecting cameras...")
            initialize_cameras()
            
            # Reconnect motors
            print("🔌 Reconnecting motor bus...")
            initialize_robot()
            
            if robot_initialized:
                print("✅ Flask server fully reconnected!")
                print(f"  - Motors: Connected")
                print(f"  - Cameras: {len(camera_captures)} active")
            else:
                print("❌ Failed to reconnect to robot")
        
        # Run in background thread
        ai_thread = threading.Thread(target=run_ai_inference)
        ai_thread.daemon = True
        ai_thread.start()
        
        return jsonify({
            'success': True,
            'message': 'AI inference started. Cameras and motors released.',
            'status': 'running'
        })
        
    except Exception as e:
        print(f"❌ Error in auto_dice_roll: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai_status', methods=['GET'])
def ai_status():
    """Check if AI is running or if server has control"""
    return jsonify({
        'server_has_control': robot_initialized,
        'robot_connected': motor_bus is not None,
        'cameras_active': len(camera_captures),
        'cameras_list': list(camera_captures.keys())
    })
    
@app.route('/api/position', methods=['GET'])
def get_position():
    global motor_bus
    if not robot_initialized or motor_bus is None:
        return jsonify({'error': 'Robot not initialized'}), 500
    
    try:
        with bus_lock:
            current_pos = read_all_motor_positions(motor_bus, MOTOR_NAMES)
        
        positions = {name: round(float(pos), 2) for name, pos in zip(MOTOR_NAMES, current_pos)}
        return jsonify(positions)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/move', methods=['POST'])
@app.route('/api/move', methods=['POST'])
def move_robot():
    """Move the robot in a specified direction (relative joint control)"""
    global motor_bus, saved_positions
    
    if not robot_initialized or motor_bus is None:
        return jsonify({'error': 'Robot not initialized'}), 500
    
    data = request.json
    direction = data.get('direction')
    
    # Set default duration
    duration = 0.5
    
    try:
        # Check if it's a saved position first
        if direction in saved_positions:
            target = saved_positions[direction]
            duration = 2.0
        else:
            with bus_lock:
                current_pos = read_all_motor_positions(motor_bus, MOTOR_NAMES)
                
            current_positions = {name: pos for name, pos in zip(MOTOR_NAMES, current_pos)}
            target = current_positions.copy()
            
            # ✅ UPDATED MOVEMENT CONTROLS
            if direction == 'forward':
                target['shoulder_lift'] -= STEP_SIZE
                target['elbow_flex'] += STEP_SIZE
            elif direction == 'backward':
                target['shoulder_lift'] += STEP_SIZE
                target['elbow_flex'] -= STEP_SIZE
                
            elif direction == 'left':
                target['shoulder_pan'] -= STEP_SIZE
            elif direction == 'right':
                target['shoulder_pan'] += STEP_SIZE
                
            elif direction == 'up':
                target['shoulder_lift'] -= STEP_SIZE
                target['elbow_flex'] -= STEP_SIZE 
            elif direction == 'down':
                target['shoulder_lift'] += STEP_SIZE
                target['elbow_flex'] += STEP_SIZE 
                
            elif direction == 'gripper_open':
                target['gripper'] = max(0, target['gripper'] - GRIPPER_STEP)
            elif direction == 'gripper_close':
                target['gripper'] = min(100, target['gripper'] + GRIPPER_STEP)
                
            elif direction == 'home':
                target = HOME_POSITION.copy()
                duration = 3.0
            else:
                return jsonify({'error': 'Invalid direction'}), 400
            
            # Clamp values
            for motor, value in target.items():
                if motor == 'gripper':
                    target[motor] = max(0, min(100, value))
                else:
                    target[motor] = max(-270, min(270, value))
        
        # Execute movement
        move_to_position_smooth(target, duration=duration)
        
        return jsonify({'success': True, 'target': target})
    
    except Exception as e:
        print(f"Error moving robot: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_position', methods=['POST'])
def save_position():
    global motor_bus, saved_positions
    if not robot_initialized or motor_bus is None:
        return jsonify({'error': 'Robot not initialized'}), 500
    
    data = request.json
    name = data.get('name')
    
    if not name:
        return jsonify({'error': 'Position name required'}), 400
    
    try:
        with bus_lock:
            current_pos = read_all_motor_positions(motor_bus, MOTOR_NAMES)
        
        positions = {name: float(pos) for name, pos in zip(MOTOR_NAMES, current_pos)}
        saved_positions[name] = positions
        save_positions_to_file()
        
        return jsonify({'success': True, 'position': positions})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_saved_positions', methods=['GET'])
def get_saved_positions():
    return jsonify(saved_positions)

if __name__ == '__main__':
    print("="*50)
    print("Starting LudoMate Remote Server")
    print("="*50)
    
    # Initialize cameras
    print("Initializing cameras...")
    initialize_cameras()
    
    # Initialize robot
    init_thread = threading.Thread(target=initialize_robot)
    init_thread.start()
    
    # Load saved positions
    load_saved_positions()
    
    try:
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        if motor_bus:
            motor_bus.disconnect()
        for cap in camera_captures.values():
            cap.release()