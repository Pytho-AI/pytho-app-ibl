#!/bin/bash

# Define variables
URL="https://s3.us-east-1.amazonaws.com/pytho.media/products/pytho_package.zip"  # Replace with the actual file URL
OUTPUT_DIR="./pytho"     # Change this to your preferred directory
ZIP_FILE="pytho_package.zip"                 # Change to the correct filename

# Check if wget is installed
if ! command -v wget &> /dev/null; then
    echo "Error: wget is not installed. Install it and try again."
    exit 1
fi

# Check if unzip is installed
if ! command -v unzip &> /dev/null; then
    echo "Error: unzip is not installed. Install it and try again."
    exit 1
fi

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# Download the file
echo "Downloading $ZIP_FILE..."
wget -O "$OUTPUT_DIR/$ZIP_FILE" "$URL"

# Check if download was successful
if [ $? -ne 0 ]; then
    echo "Error: Failed to download the file."
    exit 1
fi

# Unzip the file
echo "Unzipping $ZIP_FILE..."
unzip "$OUTPUT_DIR/$ZIP_FILE" -d "$OUTPUT_DIR"

# Check if unzip was successful
if [ $? -ne 0 ]; then
    echo "Error: Failed to unzip the file."
    exit 1
fi

# Cleanup (optional)
rm "$OUTPUT_DIR/$ZIP_FILE"

echo "Download and extraction complete!"

# Run another script at the end
SECOND_SCRIPT="$OUTPUT_DIR/pytho_package/pytho-app/scripts/install_pytho.sh"
if [ -f "$SECOND_SCRIPT" ]; then
    echo "Giving execution permission to post-install script: $SECOND_SCRIPT"
    chmod +x "$SECOND_SCRIPT"
    echo "Running post-install script: $SECOND_SCRIPT"
    bash "$SECOND_SCRIPT"
else
    echo "Post-install script not found: $SECOND_SCRIPT"
fi

