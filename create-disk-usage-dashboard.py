import re
import matplotlib.pyplot as plt
import pandas as pd

# 1. Read the CSV file
df = pd.read_csv("ec2_multi_account_ssm_mounts_report.csv")  # Replace with your actual CSV filename

# 2. Set up a 4x2 grid of subplots (8 slots total)
fig, axes = plt.subplots(4, 2, figsize=(15, 16))
axes = axes.flatten()

# 3. Iterate over each row (up to 8 rows for a 4x2 matrix)
for idx, row in df.iloc[:8].iterrows():
  ax = axes[idx]

  # Extract the last 3 columns
  provisioned = row["TotalProvisionedEBS_GB"]
  total_used = row["TotalDiskUsed_GB"]
  mount_details = str(row["MountDetails"])

  # Calculate overall percentage used relative to provisioned EBS
  overall_pct = (total_used / provisioned * 100) if provisioned > 0 else 0.0

  # Parse mount path, used GB, and percentage from MountDetails
  # Matches patterns like "/: 1.54/7.93GB (19.4%)"
  mounts = re.findall(
      r"([^|]+?):\s*([\d.]+)/[\d.]+GB\s*\(([\d.]+)%\)", mount_details
  )

  # Combine summary metrics and individual mount usage into labels, values, and percentages
  labels = ["Provisioned EBS", "Total Used"] + [m[0].strip() for m in mounts]
  values = [provisioned, total_used] + [float(m[1]) for m in mounts]
  percentages = [100.0, overall_pct] + [float(m[2]) for m in mounts]
  colors = ["#4C72B0", "#DD8452"] + ["#55A868"] * len(mounts)

  # Plot horizontal bar chart
  bars = ax.barh(labels, values, color=colors)
  ax.invert_yaxis()
  ax.set_title(
      f"Row {idx + 1}: {row['InstanceID']} ({row['Region']})",
      fontsize=11,
      fontweight="bold",
  )
  ax.set_xlabel("Storage (GB)", fontsize=9)
  ax.set_xlim(
      0, max(values) * 1.32
  )  # Extra headroom on the right for wider labels

  # Add exact GB value and percentage next to each bar
  for bar, pct in zip(bars, percentages):
    width = bar.get_width()
    ax.text(
        width + 0.15,
        bar.get_y() + bar.get_height() / 2,
        f"{width:.2f} GB ({pct:.1f}%)",
        va="center",
        fontsize=8,
    )

# 4. Hide any unused subplots
for empty_idx in range(len(df), len(axes)):
  axes[empty_idx].axis("off")

# 5. Adjust layout and save as PNG
plt.tight_layout()
plt.savefig("instance_storage_matrix-percentage.png", dpi=300)
plt.close()
print("Saved graph to instance_storage_matrix.png")
