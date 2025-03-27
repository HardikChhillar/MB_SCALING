import os
import io
import math
from flask import Flask, request, render_template_string, redirect, url_for, send_file, jsonify, session
from pdf2image import convert_from_path
import fitz  # PyMuPDF
import pandas as pd
import tempfile
import uuid
import json
from PIL import Image

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SECRET_KEY'] = 'your_secret_key_here'

# Create upload folder if it doesn't exist
if not os.path.exists(app.config['UPLOAD_FOLDER']):
    os.makedirs(app.config['UPLOAD_FOLDER'])

# Home page: upload PDF
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        file = request.files.get("pdf_file")
        if file:
            # Generate a unique filename to avoid conflicts
            filename = f"{uuid.uuid4().hex}_{file.filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            
            # Store file path in session
            session['current_pdf_path'] = filepath
            session['current_page_num'] = 0
            session['annotations'] = {}
            session['scale'] = None
            session['data_for_excel'] = []
            session['original_scale'] = None
            session['zoom_level'] = 1.5
            session['undo_stack'] = {}

            return redirect(url_for("view_page", page_num=0))
    return render_template_string(HOME_TEMPLATE)

# View a specific PDF page with annotation controls
@app.route("/page/<int:page_num>")
def view_page(page_num):
    if 'current_pdf_path' not in session:
        return redirect(url_for('index'))
    
    try:
        pdf_doc = fitz.open(session['current_pdf_path'])
        total_pages = len(pdf_doc)
        
        if page_num >= total_pages:
            page_num = total_pages - 1
        if page_num < 0:
            page_num = 0
            
        session['current_page_num'] = page_num
        
        return render_template_string(
            VIEW_PAGE_TEMPLATE,
            page_num=page_num,
            total_pages=total_pages,
            has_scale=session.get('scale') is not None,
            annotations=session.get('annotations', {}).get(str(page_num), []),
            zoom_level=session.get('zoom_level', 1.5)
        )
    except Exception as e:
        return f"Error loading PDF: {str(e)}", 500

# Endpoint to return PDF page as image
@app.route("/get_page_image/<int:page_num>")
def get_page_image(page_num):
    if 'current_pdf_path' not in session:
        return None, "No PDF loaded"
    
    try:
        zoom = session.get('zoom_level',1.5)  # Adjust zoom for better quality
        pages = convert_from_path(session['current_pdf_path'], first_page=page_num+1, last_page=page_num+1, dpi=72*zoom)
        if pages:
            img = pages[0]
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return buf, None  # Returning image byte array and None as error
    except Exception as e:
        return None, f"Error converting page: {str(e)}"
    
    return None, "No image available"

# Updated Adjust Zoom API
@app.route("/api/adjust_zoom", methods=["POST"])
def adjust_zoom():
    data = request.json
    zoom_action = data.get('action')
    
    current_zoom = session.get('zoom_level', 1.5)
    current_scale = session.get('scale')
    original_scale = session.get('original_scale')
    
    if zoom_action == 'in':
        new_zoom = min(current_zoom * 1.2, 3.0)  # Max zoom of 3x
    elif zoom_action == 'out':
        new_zoom = max(current_zoom / 1.2, 0.5)  # Min zoom of 0.5x
    else:
        return jsonify({"success": False, "error": "Invalid zoom action"}), 400
    
    # Preserve scale proportionality
    if current_scale is not None:
        # If no original scale stored, store the current scale
        if original_scale is None:
            session['original_scale'] = current_scale
            original_zoom = session.get('zoom_level', 1.5)
        else:
            original_zoom = session.get('zoom_level', 1.5)
        
        # Adjust scale proportionally to zoom change
        new_scale = original_scale * (original_zoom / new_zoom)
        session['scale'] = new_scale
    
    session['zoom_level'] = new_zoom
    
    return jsonify({
        "success": True, 
        "zoom_level": new_zoom,
        "scale": session.get('scale'),
        "message": f"Zoom set to {new_zoom:.2f}"
    })


# API to set scale
@app.route("/api/set_scale", methods=["POST"])
def set_scale():
    data = request.json
    points = data.get('points', [])
    known_distance = data.get('known_distance')
    
    if len(points) != 2 or not known_distance:
        return jsonify({"success": False, "error": "Invalid data"}), 400
    
    # Calculate pixel distance
    point1, point2 = points
    pixel_distance = math.sqrt((point2[0] - point1[0])**2 + (point2[1] - point1[1])**2)
    
    # Calculate scale (real-world units per pixel)
    scale = known_distance / pixel_distance
    session['scale'] = scale
    
    # Store scale reference as special annotation
    page_num = str(session['current_page_num'])
    annotations = session.get('annotations', {})
    
    if page_num not in annotations:
        annotations[page_num] = []
    
    # Check if there's already a scale reference and replace it
    for i, anno in enumerate(annotations.get(page_num, [])):
        if anno.get('type') == 'scale_reference':
            annotations[page_num].pop(i)
            break
    
    annotations[page_num].append({
        'type': 'scale_reference',
        'points': points,
        'label': f"Scale: {known_distance} units = {pixel_distance:.1f} pixels"
    })
    
    session['annotations'] = annotations
    
    return jsonify({
        "success": True, 
        "scale": scale,
        "message": f"Scale set: 1 pixel = {scale:.5f} units"
    })

# API to reset scale
@app.route("/api/reset_scale", methods=["POST"])
def reset_scale():
    session['scale'] = None
    
    # Remove scale reference annotation
    page_num = str(session['current_page_num'])
    annotations = session.get('annotations', {})
    
    if page_num in annotations:
        annotations[page_num] = [a for a in annotations[page_num] if a.get('type') != 'scale_reference']
        session['annotations'] = annotations
    
    return jsonify({"success": True, "message": "Scale has been reset"})

@app.route("/api/create_annotation", methods=["POST"])
def create_annotation():
    data = request.json
    annotation_type = data.get('type')
    points = data.get('points', [])
    label = data.get('label', '')

    if len(points) != 2 or not annotation_type:
        return jsonify({"success": False, "error": "Invalid data"}), 400

    page_num = str(session.get('current_page_num', data.get("page_num")))

    if "annotations" not in session:
        session["annotations"] = {}

    if page_num not in session["annotations"]:
        session["annotations"][page_num] = []

    # Track undo stack
    if page_num not in session["undo_stack"]:
        session["undo_stack"][page_num] = []

    # Get the image size
    img_byte_arr, error = get_page_image(int(page_num))
    if error:
        return jsonify({"success": False, "error": error}), 400

    img = Image.open(img_byte_arr)
    img_width, img_height = img.size  # Get actual image dimensions

    # Get the PDF page size
    pdf_path = session.get("current_pdf_path", "")
    if not pdf_path:
        return jsonify({"success": False, "error": "PDF not loaded"}), 400

    with fitz.open(pdf_path) as doc:
        pdf_page = doc[int(page_num)]
        pdf_width = pdf_page.rect.width  # Actual PDF width
        pdf_height = pdf_page.rect.height  # Actual PDF height

    # Calculate scaling factors
    scale_x = pdf_width / img_width
    scale_y = pdf_height / img_height

    # Scale the annotation points
    scaled_points = [[p[0] * scale_x, p[1] * scale_y] for p in points]

    # Categorize activity type
    rect_type = data.get("rect_type", "").lower()
    line_activities = ['wall', 'door', 'window', 'panel']
    area_activities = ['floor', 'ceiling', 'pillar']
    
    is_line_activity = rect_type in line_activities
    is_area_activity = rect_type in area_activities

    # Calculate dimensions
    p1, p2 = points
    width = abs(p2[0] - p1[0]) * session.get("scale", 1)
    height = abs(p2[1] - p1[1]) * session.get("scale", 1)
   
    # For line activities, calculate total length
    if is_line_activity:
        length = math.sqrt((p2[0] - p1[0])**2 + (p2[1] - p1[1])**2) * session.get("scale", 1)
        width = length  # Set width to total length
        height = 0  # No height for line activities
        plan_height=0
        unit = "RMT"  # Running meter
    else:
        plan_height=0
        unit = "Sqmt"  # Square meter

    # Add to Excel data
    excel_data = session.get("data_for_excel", [])
    excel_data.append([
        data.get("rect_name", f"Item {len(excel_data) + 1}"),
        data.get("parent_area", ""),
        round(width,3),
        round(height,3),
        plan_height,
        data.get("replicas", 1),
        unit,
        data.get("rect_type", "Unknown")
    ])
    session["data_for_excel"] = excel_data

    # Store annotation
    annotation = {
        "type": annotation_type,
        "points": scaled_points,
        "label": label,
        "dimensions": [width, height]
    }

    session["annotations"][page_num].append(annotation)
    session["undo_stack"][page_num].append(annotation)

    return jsonify({"success": True, "message": f"Added {annotation_type} annotation"})
# API to undo last annotation
@app.route("/api/undo_annotation", methods=["POST"])
def undo_annotation():
    page_num = str(session.get('current_page_num'))
    
    # Check if there are annotations to undo
    annotations = session.get('annotations', {}).get(page_num, [])
    undo_stack = session.get('undo_stack', {}).get(page_num, [])
    excel_data = session.get('data_for_excel', [])
    
    if not annotations or not undo_stack:
        return jsonify({"success": False, "message": "No annotations to undo"})
    
    # Remove last annotation
    last_annotation = annotations.pop()
    
    # Ensure the undo stack is updated
    if undo_stack and undo_stack[-1] == last_annotation:
        undo_stack.pop()
    
    # Remove the corresponding Excel data entry
    # This assumes the Excel data is added in the same order as annotations
    if excel_data:
        excel_data.pop()
    
    # Update session
    session['annotations'][page_num] = annotations
    session['undo_stack'][page_num] = undo_stack
    session['data_for_excel'] = excel_data
    
    return jsonify({
        "success": True, 
        "message": "Last annotation removed",
        "remaining_annotations": len(annotations),
        "remaining_excel_entries": len(excel_data)
    })

# API to clear annotations
@app.route("/api/clear_annotations", methods=["POST"])
def clear_annotations():
    page_num = str(session['current_page_num'])
    annotations = session.get('annotations', {})
    
    if page_num in annotations:
        # Keep scale reference, remove others
        scale_refs = [a for a in annotations[page_num] if a.get('type') == 'scale_reference']
        annotations[page_num] = scale_refs
        session['annotations'] = annotations
        
        return jsonify({
            "success": True, 
            "message": f"Annotations cleared from page {int(page_num) + 1}"
        })
    
    return jsonify({"success": True, "message": "No annotations to clear"})

# Export annotations to PDF
@app.route("/api/save_pdf", methods=["POST"])
def save_pdf():
    if 'current_pdf_path' not in session:
        return jsonify({"success": False, "error": "No PDF loaded"}), 400
    
    try:
        # Open the PDF
        doc = fitz.open(session['current_pdf_path'])
        annotations = session.get('annotations', {})
        
        # Apply annotations to PDF
        for page_num_str, page_annotations in annotations.items():
            page_num = int(page_num_str)
            page = doc[page_num]
          
                 # Filter out scale references and keep only meaningful annotations
            filtered_annotations = [
                anno for anno in page_annotations 
                if anno.get('type') != 'scale_reference'
            ]
            
            for anno in filtered_annotations:
                # Skip scale reference
                if anno.get('type') == 'scale_reference':
                    continue
                
                # Get points and use directly (they are already in PDF coordinates)
                points = anno.get('points', [])
                if len(points) != 2:
                    continue
                
                if anno.get('type') == 'line':
                    start, end = points
                    # Draw on PDF
                    page.draw_line(start, end, color=(1, 0, 0), width=2)
                    page.insert_text((start[0], start[1] - 10), 
                                   anno.get('label', ''), color=(0, 0, 1))
                
                elif anno.get('type') == 'square':
                    p1, p2 = points
                    
                    # Create rectangle with the two points
                    x1, y1 = p1[0], p1[1]
                    x2, y2 = p2[0], p2[1]
                    
                    # Ensure we have a proper rectangle by drawing all four sides individually
                    # This makes sure we get a complete rectangle even if PyMuPDF's rect drawing has issues
                    
                    # Top line
                    page.draw_line((x1, y1), (x2, y1), color=(0, 1, 0), width=2)
                    # Right line
                    page.draw_line((x2, y1), (x2, y2), color=(0, 1, 0), width=2)
                    # Bottom line
                    page.draw_line((x2, y2), (x1, y2), color=(0, 1, 0), width=2)
                    # Left line
                    page.draw_line((x1, y2), (x1, y1), color=(0, 1, 0), width=2)
                    
                    # Add label
                    page.insert_text((x1, y1 - 10), anno.get('label', ''), color=(0, 0, 1))
        
        # Save to temporary file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
        doc.save(temp_file.name)
        temp_file.close()
        
        # Return temporary file path to client for download
        temp_filename = os.path.basename(temp_file.name)
        session['temp_pdf'] = temp_file.name
        
        return jsonify({
            "success": True, 
            "filename": temp_filename,
            "download_url": url_for('download_pdf', filename=temp_filename)
        })
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
# Download the saved PDF
@app.route("/download/pdf/<filename>")
def download_pdf(filename):
    if 'temp_pdf' not in session:
        return "No PDF available", 404
    
    return send_file(
        session['temp_pdf'],
        as_attachment=True,
        download_name="annotated_pdf.pdf",
        mimetype="application/pdf"
    )

# Export data to Excel
@app.route("/api/save_excel", methods=["POST"])
def save_excel():
    excel_data = session.get('data_for_excel', [])
    
    if not excel_data:
        return jsonify({"success": False, "error": "No data to export"}), 400
    
    try:
        # Create DataFrame
        df = pd.DataFrame(excel_data, columns=['Name', 'Parent Area', 'Drawing Length', 'Drawing Width','Drawing Height','Drawing Number Of Replicas','Unit','Area Type'])
        
        # Save to temporary file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx')
        df.to_excel(temp_file.name, index=False)
        temp_file.close()
        
        # Return temporary file path to client for download
        temp_filename = os.path.basename(temp_file.name)
        session['temp_excel'] = temp_file.name
        
        return jsonify({
            "success": True, 
            "filename": temp_filename,
            "download_url": url_for('download_excel', filename=temp_filename)
        })
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    
# Add a new route to get data preview
@app.route("/api/get_data_preview", methods=["GET"])
def get_data_preview():
    excel_data = session.get('data_for_excel', [])
    
    if not excel_data:
        return jsonify({"success": False, "message": "No data available"})
    
    return jsonify({
        "success": True, 
        "data": excel_data
    })

# Download the saved Excel file
@app.route("/download/excel/<filename>")
def download_excel(filename):
    if 'temp_excel' not in session:
        return "No Excel file available", 404
    
    return send_file(
        session['temp_excel'],
        as_attachment=True,
        download_name="annotated_data.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

# HTML Templates
HOME_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>PDF Measurement and Annotation Tool</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 20px; background-color: #f5f5f5; }
    .upload-container { 
      width: 500px; margin: 50px auto; padding: 30px; 
      border: 1px solid #ccc; background-color: white;
      box-shadow: 0 2px 5px rgba(0,0,0,0.1);
      border-radius: 8px;
    }
    h1 { text-align: center; color: #333; }
    .form-group { margin-bottom: 20px; }
    label { display: block; margin-bottom: 5px; font-weight: bold; }
    input[type="file"] { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; }
    button { 
      background-color: #4CAF50; color: white; padding: 12px 20px; 
      border: none; cursor: pointer; width: 100%; font-size: 16px;
      border-radius: 4px;
    }
    button:hover { background-color: #45a049; }
  </style>
</head>
<body>
  <div class="upload-container">
    <h1>PDF Measurement Tool</h1>
    <form method="post" enctype="multipart/form-data">
      <div class="form-group">
        <label for="pdf_file">Select a PDF file:</label>
        <input type="file" name="pdf_file" id="pdf_file" accept="application/pdf" required>
      </div>
      <button type="submit">Upload and Open</button>
    </form>
  </div>
</body>
</html>
"""

VIEW_PAGE_TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>PDF Measurement Annotation - Page {{ page_num+1 }}</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; padding: 0; }
    .container { display: flex; flex-direction: column; height: 100vh; }
    #toolbar {
      background-color: #f0f0f0; padding: 10px; display: flex;
      justify-content: space-between; border-bottom: 1px solid #ccc;
    }
    .button-group { display: flex; gap: 10px; }
    .btn {
      padding: 8px 15px; border: none; border-radius: 4px; cursor: pointer;
      font-size: 14px; font-weight: bold;
    }
    .btn-primary { background-color: #4CAF50; color: white; }
    .btn-secondary { background-color: #f1f1f1; color: #333; border: 1px solid #ccc; }
    .btn-danger { background-color: #f44336; color: white; }
    .btn-warning { background-color: #ff9800; color: white; }
    
    #canvas-container {
      flex-grow: 1; overflow: auto; position: relative;
      background-color: #e0e0e0;
    }
    #pdfCanvas { display: block; margin: 20px auto; background-color: white; }
    #status-bar {
      background-color: #333; color: white; padding: 5px 10px;
      font-size: 14px;
    }
    .modal {
      display: none; position: fixed; z-index: 100; left: 0; top: 0;
      width: 100%; height: 100%; background-color: rgba(0,0,0,0.4);
    }
    .modal-content {
      background-color: white; margin: 15% auto; padding: 20px;
      border: 1px solid #888; width: 50%; border-radius: 5px;
    }
    .input-group { margin-bottom: 15px; }
    .input-group label { display: block; margin-bottom: 5px; }
    .input-group input { width: 100%; padding: 8px; }
    .modal-buttons { display: flex; justify-content: flex-end; gap: 10px; }
     #data-preview-modal {
      display: none;
      position: fixed;
      z-index: 100;
      left: 0;
      top: 0;
      width: 100%;
      height: 100%;
      background-color: rgba(0,0,0,0.4);
    }
    #data-preview-content {
      background-color: white;
      margin: 10% auto;
      padding: 20px;
      border: 1px solid #888;
      width: 80%;
      max-height: 70%;
      overflow-y: auto;
    }
    #data-table {
      width: 100%;
      border-collapse: collapse;
    }
    #data-table th, #data-table td {
      border: 1px solid #ddd;
      padding: 8px;
      text-align: left;
    }
    #data-table th {
      background-color: #f2f2f2;
    }
  </style>
</head>
<body>
  <div class="container">
    <div id="toolbar">
      <div class="button-group">
        <a href="{{ url_for('index') }}" class="btn btn-secondary">← Back to Home</a>
        <span>Page {{ page_num+1 }} of {{ total_pages }}</span>
        <button id="prev-btn" class="btn btn-secondary" {% if page_num == 0 %}disabled{% endif %}>
          Previous Page
        </button>
        <button id="next-btn" class="btn btn-secondary" {% if page_num == total_pages - 1 %}disabled{% endif %}>
          Next Page
        </button>
      </div>
      <div class="button-group">
        <button id="zoom-in-btn" class="btn btn-secondary">Zoom In</button>
        <button id="zoom-out-btn" class="btn btn-secondary">Zoom Out</button>
        <button id="undo-btn" class="btn btn-secondary">Undo Last</button>
        <button id="set-scale-btn" class="btn btn-primary">Set Scale</button>
        <button id="reset-scale-btn" class="btn btn-warning" {% if not has_scale %}disabled{% endif %}>Reset Scale</button>
        <button id="measure-btn" class="btn btn-primary" {% if not has_scale %}disabled{% endif %}>Add Measurement</button>
        <button id="clear-btn" class="btn btn-danger">Clear Annotations</button>
      </div>
      <div class="button-group">
        <button id="preview-data-btn" class="btn btn-secondary">Preview Data</button>
        <button id="save-pdf-btn" class="btn btn-primary">Save PDF</button>
        <button id="save-excel-btn" class="btn btn-primary">Save Data to Excel</button>
      </div>
    </div>
    
    <div id="canvas-container">
      <canvas id="pdfCanvas"></canvas>
    </div>
    
    <div id="status-bar">Ready. First click two points to set scale.</div>
  </div>
  
  <!-- Scale Setting Modal -->
  <div id="scale-modal" class="modal">
    <div class="modal-content">
      <h3>Set Scale</h3>
      <div class="input-group">
        <label for="known-distance">Enter the real-world distance between the two points:</label>
        <input type="number" id="known-distance" step="0.01" min="0.01" placeholder="e.g., 1.5 meters">
      </div>
      <div class="modal-buttons">
        <button id="cancel-scale-btn" class="btn btn-secondary">Cancel</button>
        <button id="confirm-scale-btn" class="btn btn-primary">Set Scale</button>
      </div>
    </div>
  </div>

  <!-- [Previous HTML remains the same] -->
    
    <!-- Data Preview Modal -->
    <div id="data-preview-modal">
      <div id="data-preview-content">
        <h3>Current Measurement Data</h3>
        <table id="data-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Parent Area</th>
              <th>Drawing Length</th>
              <th>Drawing Width</th>
              <th>Drawing Height</th>
              <th>Drawing Number Of Replicas</th>
              <th>Unit</th>
              <th>Area Type</th>
            </tr>
          </thead>
          <tbody id="data-table-body">
            <!-- Data rows will be dynamically populated -->
          </tbody>
        </table>
        <div style="margin-top: 15px; text-align: right;">
          <button id="close-preview-btn" class="btn btn-secondary">Close</button>
        </div>
      </div>
    </div>

  <!-- Measurement Modal -->
  <div id="measure-modal" class="modal">
    <div class="modal-content">
      <h3>Add Measurement</h3>
      <div class="input-group">
        <label for="rect-type">Type (e.g., wall, door):</label>
        <input type="text" id="rect-type" placeholder="Type">
      </div>
      <div class="input-group">
        <label for="rect-name">Name:</label>
        <input type="text" id="rect-name" placeholder="Name">
      </div>
      <!-- New section to display measurements -->
      <div id="measurements-preview" class="input-group">
        <label>Measurements:</label>
        <p id="pixel-length-display"></p>
        <p id="original-length-display"></p>
      </div>
      <div class="modal-buttons">
        <button id="cancel-measure-btn" class="btn btn-secondary">Cancel</button>
        <button id="confirm-measure-btn" class="btn btn-primary">Add</button>
      </div>
    </div>
  </div>

  <script>
     // Global variables
    const canvas = document.getElementById('pdfCanvas');
    const ctx = canvas.getContext('2d');
    let points = [];
    let currentAction = null;
    let imageObj = null;
    let annotations = {{ annotations|tojson|safe }};
    // Preview Data Button Handler
    document.getElementById('preview-data-btn').addEventListener('click', async () => {
      try {
        const response = await fetch('/api/get_data_preview', {
          method: 'GET',
          headers: {'Content-Type': 'application/json'}
        });
        const result = await response.json();
        
        if (result.success) {
          const tableBody = document.getElementById('data-table-body');
          tableBody.innerHTML = ''; // Clear previous data
          
          result.data.forEach(row => {
            const tr = document.createElement('tr');
            row.forEach(cell => {
              const td = document.createElement('td');
              td.textContent = cell;
              tr.appendChild(td);
            });
            tableBody.appendChild(tr);
          });
          
          document.getElementById('data-preview-modal').style.display = 'block';
        } else {
          updateStatus('No data available');
        }
      } catch (error) {
        console.error('Error previewing data:', error);
        updateStatus('Error retrieving data');
      }
    });

    // Close Preview Modal
    document.getElementById('close-preview-btn').addEventListener('click', () => {
      document.getElementById('data-preview-modal').style.display = 'none';
    });

    // Updated Zoom controls
    document.getElementById('zoom-in-btn').addEventListener('click', async () => {
       try {
        const response = await fetch('/api/adjust_zoom', {
         method: 'POST',
         headers: {'Content-Type': 'application/json'},
         body: JSON.stringify({ action: 'in' })
        });
        const result = await response.json();
        if (result.success) {
          updateStatus(`Zoomed in. Current zoom: ${result.zoom_level.toFixed(2)}`);
          // Reload the page image with new zoom
          location.reload();
        }
      } catch (error) {
        console.error('Zoom in error:', error);
        updateStatus('Error zooming in');
      }
    });

    document.getElementById('zoom-out-btn').addEventListener('click', async () => {
       try {
        const response = await fetch('/api/adjust_zoom', {
         method: 'POST',
         headers: {'Content-Type': 'application/json'},
         body: JSON.stringify({ action: 'out' })
        });
        const result = await response.json();
        if (result.success) {
          updateStatus(`Zoomed out. Current zoom: ${result.zoom_level.toFixed(2)}`);
          // Reload the page image with new zoom
          location.reload();
        }
      } catch (error) {
        console.error('Zoom out error:', error);
        updateStatus('Error zooming out');
      }
    });
      // Adjust zoom handler to preserve state
    async function adjustZoom(action) {
      try {
        const response = await fetch('/api/adjust_zoom', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ action: action })
        });
        const result = await response.json();
        if (result.success) {
          // Preserve important session variables
          const currentScale = result.scale;
          const currentZoom = result.zoom_level;
          
          updateStatus(`Zoomed ${action}. Current zoom: ${currentZoom.toFixed(2)}`);
          
          // Reload the page, ensuring state is preserved
          window.location.reload();
        }
      } catch (error) {
        console.error(`Zoom ${action} error:`, error);
        updateStatus(`Error zooming ${action}`);
      }
    }

    // Update zoom button event listeners
    document.getElementById('zoom-in-btn').addEventListener('click', () => adjustZoom('in'));
    document.getElementById('zoom-out-btn').addEventListener('click', () => adjustZoom('out'));

     // Undo button handler
     document.getElementById('undo-btn').addEventListener('click', async () => {
       try {
         const response = await fetch('/api/undo_annotation', {
         method: 'POST',
         headers: {'Content-Type': 'application/json'}
        });
         const result = await response.json();
         if (result.success) {
          updateStatus(result.message);
          // Remove last annotation from local annotations array
          if (annotations.length > 0) {
            annotations.pop();
          }
          redrawCanvas();
        } else {
          updateStatus(result.message);
        }
      } catch (error) {
        console.error('Undo error:', error);
        updateStatus('Error undoing last annotation');
      }
    });

    // Navigation buttons
    document.getElementById('prev-btn').addEventListener('click', () => {
      window.location.href = "{{ url_for('view_page', page_num=page_num-1) }}";
    });
    
    document.getElementById('next-btn').addEventListener('click', () => {
      window.location.href = "{{ url_for('view_page', page_num=page_num+1) }}";
    });
    
    // Button handlers
    document.getElementById('set-scale-btn').addEventListener('click', () => {
      currentAction = 'setScale';
      points = [];
      updateStatus('Click two points on the image to set the scale.');
    });
    
    document.getElementById('reset-scale-btn').addEventListener('click', async () => {
      if (confirm('Are you sure you want to reset the scale? This will not remove existing annotations.')) {
        try {
          const response = await fetch('/api/reset_scale', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
          });
          const result = await response.json();
          if (result.success) {
            updateStatus(result.message);
            document.getElementById('reset-scale-btn').disabled = true;
            document.getElementById('measure-btn').disabled = true;
            // Remove scale reference from display
            annotations = annotations.filter(a => a.type !== 'scale_reference');
            redrawCanvas();
          }
        } catch (error) {
          console.error('Error resetting scale:', error);
        }
      }
    });
    
    document.getElementById('measure-btn').addEventListener('click', () => {
      currentAction = 'measure';
      points = [];
      updateStatus('Click two points to create a measurement rectangle.');
    });
    
    document.getElementById('clear-btn').addEventListener('click', async () => {
      if (confirm('Clear all annotations on this page?')) {
        try {
          const response = await fetch('/api/clear_annotations', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'}
          });
          const result = await response.json();
          if (result.success) {
            updateStatus(result.message);
            // Keep only scale reference in annotations
            annotations = annotations.filter(a => a.type === 'scale_reference');
            redrawCanvas();
          }
        } catch (error) {
          console.error('Error clearing annotations:', error);
        }
      }
    });
    
    document.getElementById('save-pdf-btn').addEventListener('click', async () => {
      updateStatus('Saving PDF...');
      try {
        const response = await fetch('/api/save_pdf', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'}
        });
        const result = await response.json();
        if (result.success) {
          updateStatus('PDF saved. Downloading...');
          // Trigger download
          window.location.href = result.download_url;
        } else {
          updateStatus('Error saving PDF: ' + result.error);
        }
      } catch (error) {
        console.error('Error saving PDF:', error);
        updateStatus('Error saving PDF');
      }
    });
    
    document.getElementById('save-excel-btn').addEventListener('click', async () => {
      updateStatus('Exporting data to Excel...');
      try {
        const response = await fetch('/api/save_excel', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'}
        });
        const result = await response.json();
        if (result.success) {
          updateStatus('Data exported. Downloading Excel file...');
          // Trigger download
          window.location.href = result.download_url;
        } else {
          updateStatus('Error exporting data: ' + (result.error || 'No data to export'));
        }
      } catch (error) {
        console.error('Error exporting data:', error);
        updateStatus('Error exporting data');
      }
    });
    
    // Scale modal handlers
    document.getElementById('confirm-scale-btn').addEventListener('click', async () => {
      const knownDistance = parseFloat(document.getElementById('known-distance').value);
      if (isNaN(knownDistance) || knownDistance <= 0) {
        alert('Please enter a valid distance');
        return;
      }
      
      try {
        const response = await fetch('/api/set_scale', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            points: points,
            known_distance: knownDistance
          })
        });
        const result = await response.json();
        if (result.success) {
          document.getElementById('reset-scale-btn').disabled = false;
          document.getElementById('measure-btn').disabled = false;
          updateStatus(result.message);
          
          // Add scale reference to annotations
          const scaleIndex = annotations.findIndex(a => a.type === 'scale_reference');
          if (scaleIndex !== -1) {
            annotations.splice(scaleIndex, 1);
          }
          
          annotations.push({
            type: 'scale_reference',
            points: points,
            label: `Scale: ${knownDistance} units = ${
              Math.sqrt(
                Math.pow(points[1][0] - points[0][0], 2) + 
                Math.pow(points[1][1] - points[0][1], 2)
              ).toFixed(1)
            } pixels`
          });
          
          hideModal('scale-modal');
          points = [];
          currentAction = null;
          redrawCanvas();
        }
      } catch (error) {
        console.error('Error setting scale:', error);
      }
    });
    
    document.getElementById('cancel-scale-btn').addEventListener('click', () => {
      hideModal('scale-modal');
      points = [];
      currentAction = null;
      redrawCanvas();
    });
    
    // Function to categorize activity type
function categorizeActivityType(type) {
  // Convert to lowercase for case-insensitive comparison
  const lowercaseType = type.toLowerCase();
  
  // Line-based activities
  const lineActivities = ['wall', 'door', 'window', 'panel'];
  
  // Area-based activities
  const areaActivities = ['floor', 'ceiling', 'pillar'];
  
  if (lineActivities.includes(lowercaseType)) {
    return 'line';
  } else if (areaActivities.includes(lowercaseType)) {
    return 'area';
  }
  
  // Default to area if not recognized
  return 'area';
}

// Modify the confirm measure button event listener
document.getElementById('confirm-measure-btn').addEventListener('click', async () => {
  const rectType = document.getElementById('rect-type').value || 'Unknown';
  const rectName = document.getElementById('rect-name').value || `Item ${annotations.length + 1}`;
  
  // Categorize activity type
  const activityCategory = categorizeActivityType(rectType);
  
  try {
    const p1 = points[0];
    const p2 = points[1];
    const width = Math.abs(p2[0] - p1[0]);
    const height = Math.abs(p2[1] - p1[1]);
    
    // Calculate length based on canvas points
    const length = activityCategory === 'line' 
      ? Math.sqrt(width * width + height * height)  // Diagonal length for line
      : width;  // For area, use width as length
    
    const response = await fetch('/api/create_annotation', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        type: 'square',
        points: points,
        label: `${rectName} (${rectType})`,
        rect_type: rectType,
        rect_name: rectName,
        rect_height: activityCategory === 'area' ? height : 0,
        parent_area: '',
        replicas: 1,
        unit: activityCategory === 'line' ? 'running meter' : 'square meter'
      })
    });
    
    const result = await response.json();
    if (result.success) {
      updateStatus(result.message);
      
      // Modify label to show appropriate measurements
      let annotationLabel = `${rectName} (${rectType})`;
      if (activityCategory === 'line') {
        annotationLabel += ` - Length: ${(length.toFixed(2))} running meters`;
      } else {
        annotationLabel += ` - ${(width.toFixed(2))} x ${(height.toFixed(2))} square meters`;
      }
      
      // Add annotation locally
      annotations.push({
        type: 'square',
        points: points,
        label: annotationLabel
      });
      
      hideModal('measure-modal');
      points = [];
      currentAction = null;
      redrawCanvas();
    }
  } catch (error) {
    console.error('Error creating annotation:', error);
    updateStatus('Error creating annotation');
  }
});

// Modify the measurements preview to show different info based on activity type
document.getElementById('confirm-measure-btn').addEventListener('click', () => {
  const rectType = document.getElementById('rect-type').value || 'Unknown';
  const activityCategory = categorizeActivityType(rectType);
  
  const p1 = points[0];
  const p2 = points[1];
  const width = Math.abs(p2[0] - p1[0]);
  const height = Math.abs(p2[1] - p1[1]);
  
  const pixelLengthDisplay = document.getElementById('pixel-length-display');
  const originalLengthDisplay = document.getElementById('original-length-display');
  
  if (activityCategory === 'line') {
    // For line, show total length
    const length = Math.sqrt(width * width + height * height);
    pixelLengthDisplay.textContent = `Total Length (Pixels): ${length.toFixed(2)} pixels`;
    originalLengthDisplay.textContent = 'Length will be in running meters';
  } else {
    // For area, show width and height
    pixelLengthDisplay.textContent = `Pixel Dimensions: ${width.toFixed(2)} x ${height.toFixed(2)} pixels`;
    originalLengthDisplay.textContent = 'Dimensions will be in square meters';
  }
});
    
    document.getElementById('cancel-measure-btn').addEventListener('click', () => {
      hideModal('measure-modal');
      points = [];
      currentAction = null;
      redrawCanvas();
    });
    
    // Update canvas click handler
    canvas.addEventListener('click', (event) => {
      if (!currentAction) return;
      
      const rect = canvas.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      
      // Add point
      points.push([x, y]);
      
      // Draw point marker
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, 2 * Math.PI);
      ctx.fillStyle = 'orange';
      ctx.fill();
      
      if (points.length === 2) {
        // Two points collected, proceed based on current action
        if (currentAction === 'setScale') {
          showModal('scale-modal');
        } else if (currentAction === 'measure') {
          // Show measure modal and pre-populate dimensions
          const p1 = points[0];
          const p2 = points[1];
          const width = Math.abs(p2[0] - p1[0]);
          const height = Math.abs(p2[1] - p1[1]);
          
          // Update modal display
          document.getElementById('pixel-length-display').textContent = 
            `Pixel Dimensions: ${width.toFixed(2)} x ${height.toFixed(2)} pixels`;
          
          showModal('measure-modal');
        }
      }
    });
    
    // Helper functions
    function updateStatus(message) {
      document.getElementById('status-bar').textContent = message;
    }
    
    function showModal(modalId) {
      document.getElementById(modalId).style.display = 'block';
    }
    
    function hideModal(modalId) {
      document.getElementById(modalId).style.display = 'none';
    }
    
    function redrawCanvas() {
      // Clear canvas and redraw image
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      if (imageObj) {
        ctx.drawImage(imageObj, 0, 0, canvas.width, canvas.height);
        
        // Draw all annotations
        for (const anno of annotations) {
          if (anno.type === 'line') {
            drawLine(anno.points, anno.label);
          } else if (anno.type === 'square') {
            drawRect(anno.points, anno.label);
          } else if (anno.type === 'scale_reference') {
            drawScaleLine(anno.points, anno.label);
          }
        }
        
        // Draw current points if any
        for (const point of points) {
          ctx.beginPath();
          ctx.arc(point[0], point[1], 5, 0, 2 * Math.PI);
          ctx.fillStyle = 'orange';
          ctx.fill();
        }
      }
    }
    
    function drawLine(points, label) {
      if (points.length !== 2) return;
      
      const [start, end] = points;
      
      // Draw line
      ctx.beginPath();
      ctx.moveTo(start[0], start[1]);
      ctx.lineTo(end[0], end[1]);
      ctx.strokeStyle = 'red';
      ctx.lineWidth = 2;
      ctx.stroke();
      
      // Draw label
      if (label) {
        ctx.font = '12px Arial';
        ctx.fillStyle = 'blue';
        ctx.fillText(label, start[0], start[1] - 5);
      }
    }
    
    function drawScaleLine(points, label) {
      if (points.length !== 2) return;
      
      const [start, end] = points;
      
      // Draw dashed line
      ctx.beginPath();
      ctx.setLineDash([5, 3]);
      ctx.moveTo(start[0], start[1]);
      ctx.lineTo(end[0], end[1]);
      ctx.strokeStyle = 'purple';
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.setLineDash([]);
      
      // Draw endpoints
      ctx.beginPath();
      ctx.arc(start[0], start[1], 4, 0, 2 * Math.PI);
      ctx.arc(end[0], end[1], 4, 0, 2 * Math.PI);
      ctx.fillStyle = 'purple';
      ctx.fill();
      
      // Draw label
      if (label) {
        ctx.font = '12px Arial';
        ctx.fillStyle = 'purple';
        ctx.fillText(label, (start[0] + end[0]) / 2, (start[1] + end[1]) / 2 - 8);
      }
    }
    
    function drawRect(points, label) {
      if (points.length !== 2) return;
      
      const [p1, p2] = points;
      
      // Calculate dimensions
      const x = Math.min(p1[0], p2[0]);
      const y = Math.min(p1[1], p2[1]);
      const width = Math.abs(p2[0] - p1[0]);
      const height = Math.abs(p2[1] - p1[1]);
      
      // Draw rectangle
      ctx.beginPath();
      ctx.rect(x, y, width, height);
      ctx.strokeStyle = 'green';
      ctx.lineWidth = 2;
      ctx.stroke();
      
      // Draw label
      if (label) {
        ctx.font = '12px Arial';
        ctx.fillStyle = 'blue';
        ctx.fillText(label, x, y - 5);
      }
    }
    
    // Load page image
    function loadPageImage() {
      const imgUrl = "/get_page_image/{{ page_num }}";
      imageObj = new Image();
      imageObj.onload = function() {
        // Set canvas size based on image
        canvas.width = this.width;
        canvas.height = this.height;
        redrawCanvas();
      };
      imageObj.src = imgUrl;
      updateStatus("Page loaded. Ready for annotations.");
    }
    
    // Keyboard shortcuts
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        // Cancel current action
        if (currentAction) {
          currentAction = null;
          points = [];
          redrawCanvas();
          updateStatus('Action cancelled.');
        }
        // Close any open modal
        hideModal('scale-modal');
        hideModal('measure-modal');
      }
    });
    
    // Initialize
    loadPageImage();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(debug=True, port=5000)
