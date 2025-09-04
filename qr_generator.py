import qrcode
import io
import os
import time
import logging
from PIL import Image
from typing import Optional

class QRCodeGenerator:
    def __init__(self, qr_codes_dir: str = "qr_codes"):
        self.qr_codes_dir = qr_codes_dir
        os.makedirs(self.qr_codes_dir, exist_ok=True)
        self.logger = logging.getLogger(__name__)
        
        self.qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
    
    def generate_qr_code(self, data: str, size: tuple = (300, 300)) -> Optional[bytes]:
        try:
            self.qr.clear()
            self.qr.add_data(data)
            self.qr.make(fit=True)
            
            img = self.qr.make_image(fill_color="black", back_color="white")
            
            img = img.resize(size, Image.Resampling.LANCZOS)
            
            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format='PNG')
            img_byte_arr = img_byte_arr.getvalue()
            
            return img_byte_arr
        except Exception as e:
            self.logger.error(f"Ошибка при генерации QR-кода: {e}")
            return None
    
    def generate_qr_code_file(self, data: str, filename: str, size: tuple = (300, 300)) -> bool:
        try:
            self.qr.clear()
            self.qr.add_data(data)
            self.qr.make(fit=True)
            
            img = self.qr.make_image(fill_color="black", back_color="white")
            
            img = img.resize(size, Image.Resampling.LANCZOS)
            
            filepath = os.path.join(self.qr_codes_dir, filename)
            img.save(filepath, 'PNG')
            return True
        except Exception as e:
            self.logger.error(f"Ошибка при сохранении QR-кода: {e}")
            return False
    
    def generate_qr_code_in_folder(self, data: str, filename: str = None, size: tuple = (300, 300)) -> Optional[str]:
        try:
            if filename is None:
                filename = f"qr_{int(time.time())}.png"
            
            success = self.generate_qr_code_file(data, filename, size)
            if success:
                return os.path.join(self.qr_codes_dir, filename)
            return None
        except Exception as e:
            self.logger.error(f"Ошибка при генерации QR-кода в папку: {e}")
            return None
