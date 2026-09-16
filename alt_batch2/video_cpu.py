import cv2

# Open the default camera
cam = cv2.VideoCapture("/var/www/dev_aduana/alt_batch2/videos/cam1_20260901_144704.mkv")

# Get the default frame width and height
frame_width = int(cam.get(cv2.CAP_PROP_FRAME_WIDTH))
frame_height = int(cam.get(cv2.CAP_PROP_FRAME_HEIGHT))

# Define the codec and create VideoWriter object
fourcc = cv2.VideoWriter_fourcc(*'mp4v')
out = cv2.VideoWriter('output.mp4', fourcc, 20.0, (frame_width, frame_height))

while True:
    ret, frame = cam.read()
    # Factor de escala (ej. 0.5 para reducir a la mitad)
    scale_percent = 10
    width = int(frame.shape[1] * scale_percent / 100)
    height = int(frame.shape[0] * scale_percent / 100)
    dim = (width, height)

    # Redimensionar usando INTER_AREA para downscaling (mejor calidad)
    resized = cv2.resize(frame, dim, interpolation=cv2.INTER_AREA)

    # Display the captured frame
    cv2.imshow('Camera', resized)

    # Press 'q' to exit the loop
    if cv2.waitKey(1) == ord('q'):
        break

# Release the capture and writer objects
cam.release()
out.release()
cv2.destroyAllWindows()