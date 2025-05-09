r"""
This script is used to compile .ts files into .qm files for PySide6 applications.
If you want to add a new translation, you can add new .ts files in project.json. The format is "language_country" (e.g., "ko_KR").
After adding the new .ts files, run "pyside6-lupdate -project .\project.json -no-obsolete -locations none" in the terminal to generate the .ts files.
"""
import os
import subprocess

def compile_translations():
    # translations 디렉토리 내의 모든 .ts 파일을 컴파일
    for file in os.listdir('translations'):
        if file.endswith('.ts'):
            ts_file = os.path.join('translations', file)
            qm_file = os.path.join('translations', file.replace('.ts', '.qm'))
            subprocess.run(['pyside6-lrelease', ts_file, '-qm', qm_file])

if __name__ == '__main__':
    compile_translations() 