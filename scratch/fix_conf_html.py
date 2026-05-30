lines = open('templates/confirmation.html', encoding='utf-8').readlines()

# Find first </style> (correct one at 656) and second </style> (duplicate at 1205)
style_ends = []
for i, l in enumerate(lines):
    if '</style>' in l and i > 50:
        style_ends.append(i)

print('Style end lines:', [s+1 for s in style_ends])

first = style_ends[0]   # line 655 (0-indexed)
second = style_ends[1]  # line 1204 (0-indexed)

# Keep everything up to and including the first </style>, then skip to after the second </style>
clean = lines[:first+1] + lines[second+1:]
open('templates/confirmation.html', 'w', encoding='utf-8').writelines(clean)
print('Done. New line count:', len(clean))
