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